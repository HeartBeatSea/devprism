"""Markdown 渲染测试。

重点不是排版多漂亮，而是**安全**：Agent 的输出会直接塞进 innerHTML，
所以任何 HTML 片段都必须被转义。
"""

from __future__ import annotations

import unittest

from devprism.markdown_lite import render


class TestBlocks(unittest.TestCase):
    def test_heading_levels(self):
        self.assertIn("<h2>标题</h2>", render("## 标题"))
        self.assertIn("<h1>一级</h1>", render("# 一级"))

    def test_unordered_list(self):
        html = render("- 第一\n- 第二")
        self.assertIn("<ul><li>第一</li><li>第二</li></ul>", html)

    def test_ordered_list(self):
        html = render("1. 甲\n2. 乙")
        self.assertIn("<ol><li>甲</li><li>乙</li></ol>", html)

    def test_table(self):
        html = render("| 指标 | 值 |\n| --- | --- |\n| 提交 | 307 |")
        self.assertIn("<th>指标</th>", html)
        self.assertIn("<td>307</td>", html)

    def test_code_fence_is_escaped(self):
        html = render("```python\nprint('<b>')\n```")
        self.assertIn("&lt;b&gt;", html)
        self.assertNotIn("<b>", html)

    def test_blockquote_and_hr(self):
        self.assertIn("<blockquote>", render("> 引用"))
        self.assertIn("<hr>", render("---"))

    def test_paragraph(self):
        self.assertIn("<p>一句话</p>", render("一句话"))


class TestInline(unittest.TestCase):
    def test_bold_and_code(self):
        html = render("这是 **重点** 和 `code`")
        self.assertIn("<strong>重点</strong>", html)
        self.assertIn("<code>code</code>", html)

    def test_link(self):
        html = render("[文档](https://example.com)")
        self.assertIn('href="https://example.com"', html)
        self.assertIn('rel="noopener noreferrer"', html)


class TestSafety(unittest.TestCase):
    def test_script_tag_is_escaped(self):
        html = render("正常文本 <script>alert(1)</script>")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_img_onerror_is_escaped(self):
        html = render('<img src=x onerror=alert(1)>')
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_javascript_link_is_not_rendered_as_tag(self):
        # 链接语法仍会生成 a 标签，但尖括号内容已被转义，不会执行脚本
        html = render("<javascript:alert(1)>")
        self.assertNotIn("<javascript", html)


if __name__ == "__main__":
    unittest.main()
