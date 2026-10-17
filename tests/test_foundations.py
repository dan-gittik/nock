"""The pieces that replace dependencies: templates and schemas."""

from __future__ import annotations

from pathlib import Path

import pytest

from nock.errors import UsageError
from nock.schema import check_schema, validate
from nock.templates import Library, Template, TemplateError


def render(source: str, **context: object) -> str:
    return Template(source, "test").render(**context)


def test_templates_print_text_and_run_python() -> None:
    source = (
        "# Items\n"
        "!for item in items:\n"
        '    - {{ item }} {"json": {{ len(item) }}}\n'
        "!if not items:\n"
        "    None yet.\n"
        "\\![an image](x.png)\n"
    )
    assert render(source, items=["a", "bc"]) == (
        '# Items\n- a {"json": 1}\n- bc {"json": 2}\n![an image](x.png)\n'
    )
    assert render(source, items=[]) == "# Items\nNone yet.\n![an image](x.png)\n"
    assert render("{{ name!r }} != {{ 1 != 2 }}\n", name="it's") == '"it\'s" != True\n'


def test_a_blank_line_belongs_to_the_block_of_the_next_line() -> None:
    source = "Top\n!if show:\n    Shown\n\n    Still shown\n\nAfter\n"
    assert render(source, show=True) == "Top\nShown\n\nStill shown\n\nAfter\n"
    assert render(source, show=False) == "Top\n\nAfter\n"


def test_text_keeps_its_indentation_inside_blocks() -> None:
    source = "<ul>\n    !for n in range(2):\n        <li>{{ n }}</li>\n</ul>\n"
    assert render(source) == "<ul>\n    <li>0</li>\n    <li>1</li>\n</ul>\n"


def test_include_renders_in_place_at_its_indentation(tmp_path: Path) -> None:
    (tmp_path / "part.md").write_text("one\n!if flag:\n    two\n")
    (tmp_path / "page.md").write_text('Start\n  !include("part.md")\nEnd\n')
    assert Library(tmp_path).render("page.md", flag=True) == "Start\n  one\n  two\nEnd\n"


def test_template_errors_name_the_template_line() -> None:
    with pytest.raises(TemplateError, match=r"t\.md:3: .*nope"):
        Template("a\n!for x in xs:\n    {{ x.nope }}\n", "t.md").render(xs=[1])
    with pytest.raises(TemplateError, match=r"s\.md:2"):
        Template("a\n!for x in xs\n", "s.md")
    with pytest.raises(TemplateError, match="missing"):
        render("{{ missing }}\n")


def test_schemas_validate_the_subset_and_refuse_the_rest() -> None:
    schema = {
        "type": "object",
        "required": ["code"],
        "additionalProperties": False,
        "properties": {
            "code": {"type": "integer", "minimum": 100},
            "tags": {"type": "array", "items": {"enum": ["a", "b"]}, "uniqueItems": True},
            "name": {"anyOf": [{"type": "string", "pattern": "^[a-z]+$"}, {"type": "null"}]},
        },
    }
    check_schema(schema)
    validate({"code": 404, "tags": ["a"], "name": None}, schema)
    with pytest.raises(UsageError) as raised:
        validate({"code": 99, "tags": ["a", "a", "c"], "name": "X", "extra": 1}, schema)
    message = str(raised.value)
    for expected in ("less than 100", "repeated", "'c' is not one of", "none of", "unexpected"):
        assert expected in message
    with pytest.raises(UsageError, match="not of type"):
        validate({"code": True}, schema)
    with pytest.raises(UsageError, match=r"'\$ref' is not supported"):
        check_schema({"type": "object", "properties": {"a": {"$ref": "#/x"}}})
