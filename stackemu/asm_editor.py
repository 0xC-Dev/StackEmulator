"""TextArea subclass with live x86 assembly syntax highlighting.

Textual's TextArea ships tree-sitter grammars for a fixed set of
languages that does not include asm. To avoid adding a tree-sitter
dependency, this override drives Textual's internal `_highlights` map
directly from Pygments' NASM / GAS lexers. `_build_highlight_map` is
called by TextArea after every edit and on load, so live coloring
happens through the widget's normal render path.
"""

from __future__ import annotations

from textual.widgets import TextArea
from pygments.lexers.asm import NasmLexer, GasLexer
from pygments.token import Token

from .assembler import detect_syntax


# Pygments token type -> Textual highlight name (matches theme
# `syntax_styles` keys). Lookup walks up the token hierarchy so that
# children of any listed token type inherit its style.
_TOKEN_MAP = {
    Token.Comment:              "comment",
    Token.Keyword:              "keyword",
    Token.Keyword.Type:         "type",
    Token.Keyword.Reserved:     "keyword",
    Token.Keyword.Declaration:  "keyword",
    Token.Keyword.Namespace:    "keyword",
    Token.Name.Builtin:         "variable.builtin",   # registers
    Token.Name.Function:        "function",
    Token.Name.Label:           "function",
    Token.Name.Variable:        "variable.builtin",
    Token.Name.Constant:        "constant.builtin",
    Token.Name.Namespace:       "type",
    Token.Literal.Number:       "number",
    Token.Literal.Number.Float: "float",
    Token.Literal.String:       "string",
    Token.Operator:             "operator",
    Token.Operator.Word:        "keyword.operator",
    Token.Punctuation:          "punctuation.delimiter",
}


def _highlight_name(ttype) -> str | None:
    t = ttype
    while t is not None:
        if t in _TOKEN_MAP:
            return _TOKEN_MAP[t]
        t = t.parent
    return None


class AsmTextArea(TextArea):
    """TextArea with pygments-driven asm highlighting.

    Detects Intel vs AT&T from the buffer contents and swaps lexers
    on the fly so pasted source always renders in the right dialect.
    """

    def _build_highlight_map(self) -> None:
        self._line_cache.clear()
        highlights = self._highlights
        highlights.clear()

        text = self.text
        if not text:
            return

        syntax = detect_syntax(text)
        lexer = GasLexer() if syntax == "att" else NasmLexer()

        row, col = 0, 0
        try:
            tokens = list(lexer.get_tokens_unprocessed(text))
        except Exception:
            # Pygments can raise on partial / mid-edit input; degrade to
            # no highlighting rather than take down the editor.
            return

        for _index, ttype, value in tokens:
            if not value:
                continue
            name = _highlight_name(ttype)
            parts = value.split("\n")
            if len(parts) == 1:
                if name:
                    highlights[row].append((col, col + len(value), name))
                col += len(value)
            else:
                # Multi-line token: highlight from `col` to EOL on the
                # first line, all of any interior lines, then the leading
                # segment of the final line up to the new `col`.
                if name:
                    highlights[row].append((col, None, name))
                for mid in range(row + 1, row + len(parts) - 1):
                    if name:
                        highlights[mid].append((0, None, name))
                row += len(parts) - 1
                col = len(parts[-1])
                if name and col > 0:
                    highlights[row].append((0, col, name))
