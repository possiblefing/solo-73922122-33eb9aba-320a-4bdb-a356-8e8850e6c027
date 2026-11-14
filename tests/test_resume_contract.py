"""End-to-end resume ("补采缺页") contract tests for the real Crawler against
a fake upstream server (no external processes required).

Covered contracts:
  1. A round with a failing list page and a broken detail ends partial;
     resume re-fetches *only* the unfinished pages, keeps existing records,
     merges recovered details by announcement code into the same round, and
     only shows completed once every missing page/detail has succeeded.
  2. A failed list page 1 hides later pages; the resume patches the whole
     list (page 1 spawns the never-completed pages) and discovers details.
  3. Stop during a resume reclaims connections, keeps records visible, and
     leaves the round partial and resumable; while a resume runs, neither a
     new round nor a second resume may start.
  4. Resume is rejected outside a finished partial round.

Run with the project venv:
  python -I tests/test_resume_contract.py
"""

import asyncio
import sys
import time
from collections import Counter

sys.path.insert(0, "/workspace/collector")

from app.crawler import Crawler  # noqa: E402

CODES = [f"ZB-2026-{n:03d}" for n in range(1, 9)]  # 8 codes, two pages of 4


class FakeUpstream:
    """Two columns, two list pages of four items each; award items reuse the
    tender codes so cross-column dedup applies."""

    def __init__(self):
        self.inflight = 0
        self.max_inflight = 0
        self.lock = asyncio.Lock()
        self.fail_list: set[tuple[str, int]] = set()  # (section, page) -> 503
        self.broken_details: set[str] = set()  # codes with malformed detail
        self.reject_next = 0  # next N requests answered 429
        self.retry_after = 2
        self.latency = 0.1
        self.hits: Counter[str] = Counter()

    async def handle(self, target: str, send):
        path, _, query = target.partition("?")
        async with self.lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            self.hits[target] += 1
        try:
            await asyncio.sleep(self.latency)  # keep requests overlapping

            if self.reject_next > 0:
                self.reject_next -= 1
                await self._respond(send, 429, "<html>429</html>",
                                    extra=[(b"retry-after", str(self.retry_after).encode())])
                return

            if path in ("/tenders", "/awards"):
                section = "tenders" if path.startswith("/tenders") else "awards"
                page = 1
                for part in query.split("&"):
                    if part.startswith("page="):
                        page = int(part.split("=", 1)[1])
                if (section, page) in self.fail_list:
                    await self._respond(send, 503, "<html>503 temporary failure</html>")
                    return
                await self._respond(send, 200, self._list_html(section, page))
                return

            if path.startswith("/notice/"):
                code = path.rsplit("/", 1)[-1]
                if code in self.broken_details:
                    # Missing 采购单位/截止日期 rows -> parse error.
                    body = f"""<html><body><h1 id="detail-title">标题 {code}</h1>
                    <table class="detail-table">
                    <tr><th>公告编号</th><td>{code}</td></tr>
                    </table></body></html>"""
                else:
                    body = f"""<html><body><h1 id="detail-title">标题 {code}</h1>
                    <table class="detail-table">
                    <tr><th>公告编号</th><td>{code}</td></tr>
                    <tr><th>采购单位</th><td>测试单位</td></tr>
                    <tr><th>截止日期</th><td>2026-12-31 17:00</td></tr>
                    </table></body></html>"""
                await self._respond(send, 200, body)
                return

            await self._respond(send, 404, "<html>not found</html>")
        finally:
            async with self.lock:
                self.inflight -= 1

    @staticmethod
    async def _respond(send, status: int, body: str, extra=None):
        headers = [(b"content-type", b"text/html; charset=utf-8")] + list(extra or [])
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body.encode()})

    @staticmethod
    def _list_html(section: str, page: int) -> str:
        page_codes = CODES[:4] if page == 1 else CODES[4:]
        rows = "".join(
            f'<tr class="announcement"><td class="code">{c}</td>'
            f'<td><a href="/notice/{c}">公告 {c}</a></td></tr>'
            for c in page_codes
        )
        return (
            '<html><body><table id="announcement-list"><tbody>'
            + rows
            + '</tbody></table><nav class="pager" data-total-pages="2">'
            + f'<a class="page-link" href="/{section}?page=2">2</a></nav></body></html>'
        )


async def serve(upstream: FakeUpstream, port: int, ready: asyncio.Event):
    async def _pipe(reader, writer):  # noqa: ANN001
        line = await reader.readline()
        if not line:
            return
        target = line.split(b" ")[1].decode()
        # Drain headers.
        while True:
            h = await reader.readline()
            if h in (b"\r\n", b"\n", b""):
                break

        collected: dict = {}

        async def send(message):
            if message["type"] == "http.response.start":
                collected["status"] = message["status"]
                collected["headers"] = message["headers"]
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                writer.write(b"HTTP/1.1 " + str(collected["status"]).encode() + b" OK\r\n")
                for k, v in collected["headers"]:
                    writer.write(k + b": " + v + b"\r\n")
                writer.write(b"content-length: " + str(len(body)).encode() + b"\r\n\r\n")
                writer.write(body)
                await writer.drain()
                writer.close()

        await upstream.handle(target, send)

    srv = await asyncio.start_server(_pipe, "127.0.0.1", port)
    ready.set()
    async with srv:
        await srv.serve_forever()


def make_crawler(port: int) -> Crawler:
    return Crawler(
        base_url=f"http://127.0.0.1:{port}",
        sections={"招标公告": "/tenders", "中标公告": "/awards"},
    )


async def scenario_partial_then_resume(port: int) -> None:
    """List page 503 + broken detail -> partial; resume only re-fetches the
    unfinished pages and merges them into the same round."""
    upstream = FakeUpstream()
    upstream.fail_list = {("tenders", 2)}
    upstream.broken_details = {"ZB-2026-003"}
    ready = asyncio.Event()
    srv = asyncio.create_task(serve(upstream, port, ready))
    await ready.wait()
    try:
        crawler = make_crawler(port)
        rid = await crawler.start_round()
        await crawler._round_task
        snap = crawler.snapshot()
        kinds = sorted((p["page_type"], p["section"], p["page"]) for p in snap["unfinished"])
        print(f"[1] round: status={snap['status']} records={snap['records_count']} "
              f"unfinished={kinds}")
        assert snap["status"] == "partial"
        # 8 unique codes minus the 4 hidden behind tenders?page=2... but the
        # award column re-exposes them, so only the broken detail is missing.
        assert snap["records_count"] == 7
        assert kinds == [("detail", "招标公告", "ZB-2026-003"), ("list", "招标公告", "2")]

        hits_after_round = Counter(upstream.hits)

        # Resume without fixing anything: still partial, records kept.
        rid2 = await crawler.start_resume()
        assert rid2 == rid, "resume must keep the original round id"
        await crawler._round_task
        snap = crawler.snapshot()
        print(f"[1] resume#1 (unfixed): status={snap['status']} records={snap['records_count']} "
              f"unfinished={len(snap['unfinished'])} resume_count={snap['resume_count']}")
        assert snap["status"] == "partial"
        assert snap["records_count"] == 7  # 已有公告保持可见
        assert len(snap["unfinished"]) == 2
        assert snap["resume_count"] == 1
        assert snap["round_id"] == rid

        # Fix the site, resume again: everything patches into the same round.
        upstream.fail_list.clear()
        upstream.broken_details.clear()
        hits_before_fixed_resume = Counter(upstream.hits)
        await crawler.start_resume()
        await crawler._round_task
        snap = crawler.snapshot()
        print(f"[1] resume#2 (fixed): status={snap['status']} records={snap['records_count']} "
              f"unfinished={len(snap['unfinished'])} max_inflight={upstream.max_inflight}")
        assert snap["status"] == "completed"
        assert snap["records_count"] == 8
        assert not snap["unfinished"]
        assert snap["round_id"] == rid
        assert upstream.max_inflight <= 2

        # Only the unfinished pages were re-fetched: each exactly once more.
        assert upstream.hits["/tenders?page=2"] == hits_before_fixed_resume["/tenders?page=2"] + 1
        assert upstream.hits["/notice/ZB-2026-003"] == hits_before_fixed_resume["/notice/ZB-2026-003"] + 1
        # Pages that succeeded in the original round were never re-requested
        # (page 1 is requested as the bare section path, without ?page=1).
        for target in ("/tenders", "/awards", "/awards?page=2",
                       "/notice/ZB-2026-001", "/notice/ZB-2026-005", "/notice/ZB-2026-008"):
            assert upstream.hits[target] == hits_after_round[target], (
                f"{target} re-fetched during resume: {hits_after_round[target]} -> {upstream.hits[target]}"
            )
        # Records merged by code: no duplicates, content intact.
        codes = sorted(r["announcement_code"] for r in crawler.records())
        assert codes == CODES
        assert all(r["procurement_unit"] == "测试单位" for r in crawler.records())
        print("[1] OK: partial -> resume -> completed, only missing pages re-fetched")
    finally:
        srv.cancel()


async def scenario_list_patching(port: int) -> None:
    """Failed page 1 hides later pages; the resume must patch the whole list
    and discover the details behind it."""
    upstream = FakeUpstream()
    upstream.fail_list = {("tenders", 1), ("awards", 2)}
    ready = asyncio.Event()
    srv = asyncio.create_task(serve(upstream, port, ready))
    await ready.wait()
    try:
        crawler = make_crawler(port)
        await crawler.start_round()
        await crawler._round_task
        snap = crawler.snapshot()
        kinds = sorted((p["page_type"], p["section"], p["page"]) for p in snap["unfinished"])
        print(f"[2] round: status={snap['status']} records={snap['records_count']} "
              f"unfinished={kinds}")
        assert snap["status"] == "partial"
        # Only awards page 1 succeeded -> details 001..004.
        assert snap["records_count"] == 4
        assert kinds == [("list", "中标公告", "2"), ("list", "招标公告", "1")]
        assert upstream.hits["/tenders?page=2"] == 0  # never discovered

        upstream.fail_list.clear()
        awards_p2_hits = upstream.hits["/awards?page=2"]  # 3 failed attempts so far
        await crawler.start_resume()
        await crawler._round_task
        snap = crawler.snapshot()
        print(f"[2] resume: status={snap['status']} records={snap['records_count']} "
              f"tenders_p2_hits={upstream.hits['/tenders?page=2']}")
        assert snap["status"] == "completed"
        assert snap["records_count"] == 8
        assert not snap["unfinished"]
        # The resume patched the never-completed list pages...
        assert upstream.hits["/tenders?page=2"] == 1
        assert upstream.hits["/awards?page=2"] == awards_p2_hits + 1
        # ...and discovered the details hidden behind them.
        assert all(upstream.hits[f"/notice/{c}"] == 1 for c in CODES[4:])
        # Guard: a completed round is not resumable.
        try:
            await crawler.start_resume()
            raise AssertionError("resume on a completed round must fail")
        except RuntimeError as exc:
            print(f"[2] resume on completed rejected: {exc}")
        print("[2] OK: list fully patched, new details discovered and merged")
    finally:
        srv.cancel()


async def scenario_stop_during_resume(port: int) -> None:
    """Stop reclaims a resume; the round stays partial and resumable; no new
    round or duplicate resume may start while a resume runs."""
    upstream = FakeUpstream()
    upstream.broken_details = {"ZB-2026-003"}
    ready = asyncio.Event()
    srv = asyncio.create_task(serve(upstream, port, ready))
    await ready.wait()
    try:
        crawler = make_crawler(port)
        await crawler.start_round()
        await crawler._round_task
        snap = crawler.snapshot()
        assert snap["status"] == "partial" and snap["records_count"] == 7
        assert [p["page"] for p in snap["unfinished"]] == ["ZB-2026-003"]

        # The resume's first requests hit 429 so it cools down mid-flight.
        upstream.reject_next = 2
        await crawler.start_resume()
        # While the resume runs: no new round, no duplicate resume.
        for action, name in ((crawler.start_round, "new round"),
                             (crawler.start_resume, "duplicate resume")):
            try:
                await action()
                raise AssertionError(f"{name} must be rejected during a resume")
            except RuntimeError as exc:
                print(f"[3] {name} during resume rejected: {exc}")

        await asyncio.sleep(0.4)  # let the first 429 land and cooldown start
        hits_at_stop = sum(upstream.hits.values())
        t0 = time.monotonic()
        await crawler.stop_round()
        stop_elapsed = time.monotonic() - t0
        snap = crawler.snapshot()
        print(f"[3] stopped resume: status={snap['status']} records={snap['records_count']} "
              f"unfinished={len(snap['unfinished'])} stop_elapsed={stop_elapsed:.2f}s")
        # 补采被停止：已有公告保持可见，未完成页仍可继续补采。
        assert snap["status"] == "partial"
        assert snap["records_count"] == 7
        assert [p["page"] for p in snap["unfinished"]] == ["ZB-2026-003"]
        assert stop_elapsed < 2.0, "stop must reclaim promptly, not wait out the cooldown"
        await asyncio.sleep(0.3)
        assert sum(upstream.hits.values()) == hits_at_stop, "requests leaked after stop"

        # Site recovers but the first resumed request still draws a 429: the
        # resume must observe the site-wide cooldown and then finish.
        upstream.broken_details.clear()
        upstream.reject_next = 1
        t0 = time.monotonic()
        await crawler.start_resume()
        await crawler._round_task
        elapsed = time.monotonic() - t0
        snap = crawler.snapshot()
        print(f"[3] resume after stop: status={snap['status']} records={snap['records_count']} "
              f"resume_count={snap['resume_count']} elapsed={elapsed:.1f}s")
        assert snap["status"] == "completed"
        assert snap["records_count"] == 8
        assert snap["resume_count"] == 2
        assert elapsed >= upstream.retry_after, "resume ignored the 429 cooldown"
        print("[3] OK: stopped resume stays partial, records kept, resumable")
    finally:
        srv.cancel()


async def scenario_guards(port: int) -> None:
    upstream = FakeUpstream()
    ready = asyncio.Event()
    srv = asyncio.create_task(serve(upstream, port, ready))
    await ready.wait()
    try:
        crawler = make_crawler(port)
        try:
            await crawler.start_resume()
            raise AssertionError("resume from idle must fail")
        except RuntimeError as exc:
            print(f"[4] resume from idle rejected: {exc}")

        await crawler.start_round()
        await crawler._round_task
        assert crawler.snapshot()["status"] == "completed"
        try:
            await crawler.start_resume()
            raise AssertionError("resume on completed must fail")
        except RuntimeError as exc:
            print(f"[4] resume on completed rejected: {exc}")
        print("[4] OK: resume only opens for finished partial rounds")
    finally:
        srv.cancel()


async def main() -> None:
    await scenario_partial_then_resume(18101)
    await scenario_list_patching(18102)
    await scenario_stop_during_resume(18103)
    await scenario_guards(18104)
    print("\nALL RESUME CONTRACT TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
