"""符号抽取测试。"""

from __future__ import annotations

import unittest

from devprism.symbols import extract_symbols, language_of, summarize_symbols

JAVA = """
package demo;

public class Service {
    private final Repo repo;

    public Service(Repo repo) {
        this.repo = repo;
    }

    public String describe(int id) {
        return repo.find(id).name();
    }

    private void helper() {}
}
"""

PYTHON = """
class Client:
    def fetch(self, url):
        pass

    async def close(self):
        pass

def main():
    pass
"""


class TestLanguageDetection(unittest.TestCase):
    def test_known_extensions(self):
        self.assertEqual(language_of("a/B.java"), "java")
        self.assertEqual(language_of("a/b.py"), "python")
        self.assertEqual(language_of("x.tsx"), "typescript")

    def test_unknown_returns_none(self):
        self.assertIsNone(language_of("README.md"))
        self.assertIsNone(language_of("data.bin"))


class TestExtractSymbols(unittest.TestCase):
    def test_java_class_and_methods(self):
        symbols = extract_symbols("Service.java", JAVA)
        names = {s["name"] for s in symbols}
        kinds = {s["kind"] for s in symbols}

        self.assertIn("Service", names)
        self.assertIn("describe", names)
        self.assertIn("helper", names)
        self.assertEqual(kinds, {"class", "method"})

    def test_java_line_numbers(self):
        symbols = extract_symbols("Service.java", JAVA)
        service = next(s for s in symbols if s["name"] == "Service")
        self.assertEqual(service["line"], 4)

    def test_python(self):
        symbols = extract_symbols("client.py", PYTHON)
        found = {(s["kind"], s["name"]) for s in symbols}

        self.assertIn(("class", "Client"), found)
        self.assertIn(("function", "fetch"), found)
        self.assertIn(("function", "close"), found)
        self.assertIn(("function", "main"), found)

    def test_comments_are_skipped(self):
        symbols = extract_symbols("a.py", "# class Fake:\nclass Real:\n    pass\n")

        self.assertEqual([s["name"] for s in symbols], ["Real"])

    def test_unknown_language_is_empty(self):
        self.assertEqual(extract_symbols("README.md", "# 标题"), [])

    def test_duplicate_names_are_collapsed(self):
        text = "public class A {\n  public void run() {}\n}\npublic class A2 {\n  public void run() {}\n}\n"
        symbols = extract_symbols("A.java", text)
        names = [s["name"] for s in symbols]

        self.assertEqual(names.count("run"), 1, "同名方法只保留第一次出现")


class TestSummarize(unittest.TestCase):
    def test_counts_types_and_functions(self):
        symbols = extract_symbols("Service.java", JAVA)
        text = summarize_symbols(symbols)

        self.assertIn("个类型", text)
        self.assertIn("个函数", text)

    def test_empty(self):
        self.assertEqual(summarize_symbols([]), "未识别到符号")


if __name__ == "__main__":
    unittest.main()
