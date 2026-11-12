"""Regression tests for the requirement-list diff rules.

Run with:  python -m unittest discover -s tests -v
These tests use httpx's in-process mock transport, so no network or demo
container is required.
"""

from __future__ import annotations

import unittest

import httpx

from app.listdiff import diff_lists, parse_requirements


HEADER = "<tr><th>包号</th><th>品目号</th><th>品目名称</th><th>数量</th><th>单位</th></tr>"


def page(table_body: str, title: str = "采购公告", head: str = "") -> str:
    return f"""
    <html><head><title>{title}</title></head>
    <body><article>
      <h1>{title}</h1>
      {head}
      <table>{table_body}</table>
    </article></body></html>
    """


def req_page(rows: list[tuple[str | None, str, str, str, str]], title: str = "采购公告") -> str:
    """Build a detail page whose package column uses rowspan merges."""

    body_rows = []
    index = 0
    while index < len(rows):
        package, item, name, qty, unit = rows[index]
        if package is None:
            body_rows.append(
                f"<tr><td>{item}</td><td>{name}</td><td>{qty}</td><td>{unit}</td></tr>"
            )
            index += 1
            continue
        span = 1
        while index + span < len(rows) and rows[index + span][0] is None:
            span += 1
        body_rows.append(
            f'<tr><td rowspan="{span}">{package}</td>'
            f"<td>{item}</td><td>{name}</td><td>{qty}</td><td>{unit}</td></tr>"
        )
        index += 1
    return page(HEADER + "".join(body_rows), title=title)


class DiffFetchTests(unittest.IsolatedAsyncioTestCase):
    def make_factory(self, routes: dict[str, str], failing: set[str] | None = None):
        failing = failing or set()

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path in failing:
                return httpx.Response(503, text="service unavailable")
            if path not in routes:
                return httpx.Response(404, text="not found")
            return httpx.Response(
                200, text=routes[path], headers={"content-type": "text/html"}
            )

        def factory():
            return httpx.AsyncClient(
                transport=httpx.MockTransport(handler), follow_redirects=True
            )

        return factory

    async def test_row_order_change_is_not_a_modification(self):
        old = req_page(
            [
                ("1", "1", "台式计算机", "50", "台"),
                (None, "2", "便携式计算机", "20", "台"),
                ("2", "1", "复印纸", "200", "箱"),
            ]
        )
        new = req_page(
            [
                ("2", "1", "复印纸", "200", "箱"),
                ("1", "2", "便携式计算机", "20", "台"),
                (None, "1", "台式计算机", "50", "台"),
            ]
        )
        routes = {"/old": old, "/new": new}
        result = (
            await diff_lists(
                "http://test/old", "http://test/new",
                client_factory=self.make_factory(routes),
            )
        ).as_dict()
        self.assertTrue(result["determinable"])
        self.assertEqual(result["summary"]["unchanged"], 3)
        self.assertEqual(result["summary"]["modified"], 0)
        self.assertEqual(result["summary"]["added"], 0)
        self.assertEqual(result["summary"]["removed"], 0)

    async def test_modification_move_add_and_remove(self):
        old = req_page(
            [
                ("1", "1", "台式计算机", "50", "台"),
                (None, "2", "便携式计算机", "20", "台"),
                (None, "3", "激光打印机", "10", "台"),
                ("2", "1", "复印纸", "200", "箱"),
                (None, "2", "硒鼓", "80", "支"),
            ]
        )
        new = req_page(
            [
                ("1", "2", "便携式计算机", "20", "台"),
                (None, "1", "台式计算机", "60", "台"),
                ("2", "1", "复印纸", "200", "箱"),
                (None, "3", "激光打印机", "10", "台"),
                (None, "4", "扫描仪", "5", "台"),
            ]
        )
        routes = {"/old": old, "/new": new}
        result = (
            await diff_lists(
                "http://test/old", "http://test/new",
                client_factory=self.make_factory(routes),
            )
        ).as_dict()
        self.assertTrue(result["determinable"])
        self.assertEqual(result["summary"]["unchanged"], 2)
        # 数量 50 -> 60 台
        self.assertEqual(len(result["modified"]), 1)
        entry = result["modified"][0]
        self.assertEqual((entry["package_no"], entry["item_no"]), ("1", "1"))
        self.assertEqual(
            entry["changes"], [{"field": "数量", "old": "50", "new": "60"}]
        )
        # 换包：旧包 1 删除品目 3，新包 2 新增品目 3；另有删除与新增
        removed_keys = {(r["package_no"], r["item_no"]) for r in result["removed"]}
        added_keys = {(r["package_no"], r["item_no"]) for r in result["added"]}
        self.assertEqual(removed_keys, {("1", "3"), ("2", "2")})
        self.assertEqual(added_keys, {("2", "3"), ("2", "4")})
        self.assertTrue(all(r["source_url"] for r in result["added"]))
        self.assertTrue(all(r["source_url"] for r in result["removed"]))

    async def test_conflict_marks_only_that_item_pending(self):
        old = req_page(
            [
                ("1", "1", "订书机", "100", "个"),
                (None, "2", "文件柜", "15", "组"),
                ("2", "1", "白板", "8", "块"),
            ]
        )
        # 新版中 (包1, 品目2) 出现两行且数量冲突
        new_table = (
            HEADER
            + '<tr><td rowspan="3">1</td><td>1</td><td>订书机</td><td>120</td><td>个</td></tr>'
            + "<tr><td>2</td><td>文件柜</td><td>15</td><td>组</td></tr>"
            + "<tr><td>2</td><td>文件柜</td><td>20</td><td>组</td></tr>"
            + '<tr><td>2</td><td>1</td><td>白板</td><td>8</td><td>块</td></tr>'
        )
        routes = {"/old": old, "/new": page(new_table)}
        result = (
            await diff_lists(
                "http://test/old", "http://test/new",
                client_factory=self.make_factory(routes),
            )
        ).as_dict()
        self.assertTrue(result["determinable"])
        # 其余品目照常比较：订书机修改、白板不变
        self.assertEqual(len(result["modified"]), 1)
        self.assertEqual(result["modified"][0]["item_no"], "1")
        self.assertEqual(result["summary"]["unchanged"], 1)
        # 冲突品目仅待核，不产生增删
        self.assertEqual(result["summary"]["added"], 0)
        self.assertEqual(result["summary"]["removed"], 0)
        self.assertEqual(len(result["pending"]), 1)
        pending = result["pending"][0]
        self.assertEqual((pending["package_no"], pending["item_no"]), ("1", "2"))
        self.assertEqual(len(pending["new_rows"]), 2)
        self.assertEqual(len(pending["old_rows"]), 1)

    async def test_conflict_does_not_imply_removal(self):
        # 旧版冲突、新版无此键：只能待核，不能算删除
        old_table = (
            HEADER
            + '<tr><td rowspan="2">1</td><td>1</td><td>订书机</td><td>100</td><td>个</td></tr>'
            + "<tr><td>1</td><td>订书机</td><td>150</td><td>个</td></tr>"
        )
        new = req_page([("1", "2", "文件柜", "15", "组")])
        routes = {"/old": page(old_table), "/new": new}
        result = (
            await diff_lists(
                "http://test/old", "http://test/new",
                client_factory=self.make_factory(routes),
            )
        ).as_dict()
        self.assertTrue(result["determinable"])
        self.assertEqual(result["summary"]["removed"], 0)
        self.assertEqual(len(result["pending"]), 1)
        self.assertEqual(result["pending"][0]["item_no"], "1")
        self.assertEqual(len(result["pending"][0]["old_rows"]), 2)
        self.assertEqual(len(result["pending"][0]["new_rows"]), 0)
        # 新版独有的品目仍然正常计为新增
        self.assertEqual(result["summary"]["added"], 1)

    async def test_identical_duplicate_rows_are_deduplicated(self):
        old_table = (
            HEADER
            + '<tr><td rowspan="2">1</td><td>1</td><td>订书机</td><td>100</td><td>个</td></tr>'
            + "<tr><td>1</td><td>订书机</td><td>100</td><td>个</td></tr>"
        )
        new = req_page([("1", "1", "订书机", "100", "个")])
        routes = {"/old": page(old_table), "/new": new}
        result = (
            await diff_lists(
                "http://test/old", "http://test/new",
                client_factory=self.make_factory(routes),
            )
        ).as_dict()
        self.assertTrue(result["determinable"])
        self.assertEqual(result["summary"]["pending"], 0)
        self.assertEqual(result["summary"]["unchanged"], 1)
        self.assertTrue(result["warnings"])

    async def test_fetch_failure_yields_no_confirmed_diff(self):
        old = req_page([("1", "1", "订书机", "100", "个")])
        routes = {"/old": old, "/new": old}
        result = (
            await diff_lists(
                "http://test/old", "http://test/new",
                client_factory=self.make_factory(routes, failing={"/new"}),
            )
        ).as_dict()
        self.assertFalse(result["determinable"])
        self.assertIn("http://test/new", [f["url"] for f in result["failures"]])
        self.assertEqual(result["added"], [])
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["modified"], [])
        self.assertEqual(result["pending"], [])

    async def test_page_without_requirement_table_is_a_failure(self):
        ok = req_page([("1", "1", "订书机", "100", "个")])
        no_table = page("", head="<p>本公告不含需求表。</p>")
        routes = {"/old": ok, "/new": no_table}
        result = (
            await diff_lists(
                "http://test/old", "http://test/new",
                client_factory=self.make_factory(routes),
            )
        ).as_dict()
        self.assertFalse(result["determinable"])
        self.assertIn("http://test/new", [f["url"] for f in result["failures"]])
        self.assertEqual(result["modified"], [])

    async def test_cross_site_addresses_are_rejected(self):
        routes = {}
        result = (
            await diff_lists(
                "http://a.example/old", "http://b.example/new",
                client_factory=self.make_factory(routes),
            )
        ).as_dict()
        self.assertFalse(result["determinable"])
        self.assertEqual(result["failures"][0]["stage"], "validation")


class ParseTests(unittest.TestCase):
    def test_rowspan_package_numbers_are_restored(self):
        html = req_page(
            [
                ("1", "1", "台式计算机", "50", "台"),
                (None, "2", "便携式计算机", "20", "台"),
                ("2", "1", "复印纸", "200", "箱"),
            ]
        )
        parsed = parse_requirements(html, "http://test/x")
        self.assertEqual(parsed.table_count, 1)
        self.assertEqual(
            [(row.package_no, row.item_no) for row in parsed.rows],
            [("1", "1"), ("1", "2"), ("2", "1")],
        )

    def test_colspan_title_and_note_rows_are_skipped(self):
        table = (
            '<tr><th colspan="5">采购需求一览表</th></tr>'
            + HEADER
            + '<tr><td rowspan="2">1</td><td>1</td><td>台式计算机</td><td>50</td><td>台</td></tr>'
            + "<tr><td>2</td><td>便携式计算机</td><td>20</td><td>台</td></tr>"
            + '<tr><td colspan="5">注：以上数量为预估值。</td></tr>'
        )
        parsed = parse_requirements(page(table), "http://test/x")
        self.assertEqual(len(parsed.rows), 2)
        self.assertEqual(parsed.rows[1].package_no, "1")

    def test_blank_package_cells_inherit_previous_value(self):
        table = (
            HEADER
            + "<tr><td>1</td><td>1</td><td>台式计算机</td><td>50</td><td>台</td></tr>"
            + "<tr><td></td><td>2</td><td>便携式计算机</td><td>20</td><td>台</td></tr>"
        )
        parsed = parse_requirements(page(table), "http://test/x")
        self.assertEqual([row.package_no for row in parsed.rows], ["1", "1"])

    def test_item_numbers_are_canonicalised(self):
        old = req_page([("1", "01", "台式计算机", "50", "台")])
        new = req_page([("1", "1", "台式计算机", "50", "台")])
        parsed_old = parse_requirements(old, "http://test/old")
        parsed_new = parse_requirements(new, "http://test/new")
        self.assertEqual(parsed_old.rows[0].key, parsed_new.rows[0].key)


if __name__ == "__main__":
    unittest.main()
