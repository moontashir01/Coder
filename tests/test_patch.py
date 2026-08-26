"""The surgical-edit matching ladder (app/agent/patch.py).

Pure text in, text out — no model, no disk. Each rung of the ladder is pinned
separately, and so is every case where a rung must REFUSE: a wrong match is
silent, a refused one is reported and retried, so the refusals are the half
that keeps the tolerance safe to have.
"""

from app.agent.patch import (
    apply_block,
    apply_edits,
    find_block,
    is_catastrophic_shrink,
    nearest_region,
    numbered,
    strip_line_numbers,
)

# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def test_rung1_exact_substring():
    assert apply_block("a = 1\nb = 2\n", "b = 2", "b = 99") == "a = 1\nb = 99\n"


def test_rung2_ignores_trailing_whitespace():
    out = apply_block("def f():\n    return 1\n", "    return 1   ", "    return 2")
    assert out == "def f():\n    return 2\n"


def test_rung3_reindents_when_search_drops_indentation():
    content = 'def greet(name):\n    return f"Hello {name}"\n'
    out = apply_block(content, 'return f"Hello {name}"', 'return f"Goodbye {name}"')
    assert out == 'def greet(name):\n    return f"Goodbye {name}"\n'


def test_rung4_matches_a_misquoted_multiline_block():
    """The 7B failure this rung exists for: right lines, mangled inner spacing."""
    content = "def total(items):\n    n = sum(x.price  for x in items)\n    return n\n"
    search = "def total( items ):\n    n = sum(x.price for x in items)\n    return n"
    out = apply_block(content, search, "def total(items):\n    return 0")
    assert out == "def total(items):\n    return 0\n"


def test_rung4_refuses_a_single_line_search():
    """One line is not an anchor — `return None` appears everywhere."""
    content = "def a():\n    return  None\ndef b():\n    return  None\n"
    assert apply_block(content, "return None", "return 0") is None


def test_rung4_refuses_two_equally_good_candidates():
    """Two fuzzy hits is a coin flip, and a wrong replacement is silent."""
    block = "def handler(request):\n    value = request.get( 'x' )\n    return value\n"
    content = block + "\n" + block
    search = "def handler(request):\n    value = request.get('x')\n    return value"
    assert apply_block(content, search, "REPLACED") is None


def test_a_search_that_is_nowhere_near_the_file_fails():
    assert apply_block("alpha\nbeta\n", "zeta\nomega", "x") is None


def test_find_block_reports_which_rung_matched():
    content = "def f():\n    return 1\n"
    assert find_block(content, "    return 1  ").tier == "trailing-ws"
    assert find_block(content, "return 1").tier == "indent"


def test_a_search_longer_than_the_file_is_refused():
    assert find_block("one line\n", "a\nb\nc\nd\ne") is None


# ---------------------------------------------------------------------------
# The line-number gutter
# ---------------------------------------------------------------------------


def test_numbered_lines_are_stripped_back_off_a_search_block():
    """Showing a gutter is only safe because quoting one back is undone."""
    content = "def f():\n    return 1\n"
    quoted = "   1 | def f():\n   2 |     return 1"
    assert apply_block(content, quoted, "def f():\n    return 2") == (
        "def f():\n    return 2\n"
    )


def test_a_pipe_in_real_code_is_not_mistaken_for_a_gutter():
    src = "flags = A | B\nmask = 1 | 2\n"
    assert strip_line_numbers(src) == src


def test_out_of_order_numbers_are_not_a_gutter():
    assert strip_line_numbers("9 | a\n2 | b") == "9 | a\n2 | b"


def test_numbered_renders_a_gutter():
    assert numbered("a\nb", start=4) == "   4 | a\n   5 | b"


# ---------------------------------------------------------------------------
# The failed-edit report
# ---------------------------------------------------------------------------


def test_nearest_region_shows_the_text_the_search_came_closest_to():
    content = "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"
    near = nearest_region(content, "def beta():\n    return TWO")
    assert "def beta():" in near
    assert " | " in near  # numbered, so the model can see where it is


def test_nearest_region_says_nothing_when_nothing_is_close():
    assert nearest_region("alpha\nbeta\n", "zzzzzz\nqqqqqq") == ""


# ---------------------------------------------------------------------------
# apply_edits
# ---------------------------------------------------------------------------


def test_apply_edits_names_which_edit_missed():
    content = "a = 1\nb = 2\nc = 3\n"
    new, applied, failed = apply_edits(content, [("a = 1", "a = 9"), ("zz", "y")])
    assert applied == [0] and failed == [1]
    assert new == "a = 9\nb = 2\nc = 3\n"


def test_apply_edits_compose_in_order():
    new, applied, failed = apply_edits("x\n", [("x", "y"), ("y", "z")])
    assert (new, applied, failed) == ("z\n", [0, 1], [])


# ---------------------------------------------------------------------------
# The shrink guard
# ---------------------------------------------------------------------------


def test_a_truncated_rewrite_is_caught():
    old = "line\n" * 200
    assert is_catastrophic_shrink(old, "line\n" * 10) is True


def test_a_small_file_may_legitimately_shrink():
    assert is_catastrophic_shrink("a = 1\nb = 2\n", "a = 1\n") is False


def test_a_requested_deletion_is_not_a_truncation():
    old = "line\n" * 200
    assert is_catastrophic_shrink(old, "line\n", "remove the old handlers") is False


def test_an_empty_result_is_always_a_truncation():
    old = "line\n" * 200
    assert is_catastrophic_shrink(old, "   ", "delete everything") is True


def test_an_ordinary_edit_is_not_a_shrink():
    old = "line\n" * 200
    assert is_catastrophic_shrink(old, "line\n" * 199) is False
