import pathlib
import random

import human

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_keystroke_pace_varies_the_way_a_hand_does():
    rng = random.Random(7)
    values = human.intervals("Plymouth lies buried under volcanic deposits.", rng)
    assert len(values) == 45
    assert min(values) >= human.KEY_MINIMUM_MS
    # A metronome is the tell: Studio's old path typed with delay=0, so the
    # keystrokes had no gap between them at all.
    spread = max(values) - min(values)
    assert spread > 60, f"Keystroke pace is too regular to pass for a person: {spread} ms"
    assert len(set(values)) > 20


def test_text_splits_into_what_a_keyboard_can_produce_and_what_it_cannot():
    runs = human.segments("Montserrat \U0001F334 today")
    assert "".join(run for _, run in runs) == "Montserrat \U0001F334 today"
    assert [typable for typable, _ in runs] == [True, False, True]
    assert human.segments("plain ascii only") == [(True, "plain ascii only")]


def test_a_person_types_what_they_know_and_pastes_what_they_composed():
    assert human.would_type("Plymouth lies buried.")
    assert not human.would_type("x" * (human.TYPED_LIMIT + 1))
    assert not human.would_type("first line\nsecond line")
    assert not human.would_type("Montserrat \U0001F334")


def test_clipboard_round_trip_and_restore():
    if not human.clipboard_tools():
        return  # no clipboard on this platform; enter_text falls back to typing
    before = human.read_clipboard()
    assert human.write_clipboard("caribbean awesome probe")
    assert human.read_clipboard() == "caribbean awesome probe"
    if before is not None:
        human.write_clipboard(before)
        assert human.read_clipboard() == before


def test_no_fabricated_browser_events_anywhere_in_the_publisher():
    """A page can tell a dispatched event from a real one; never send one.

    Studio's controls used to be driven by dispatching MouseEvent sequences
    into the shadow DOM, and the upload fields by execCommand('insertText').
    Both arrive without anyone touching the mouse or the keyboard.
    """
    import ast

    offenders = []
    for path in sorted(REPO.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        # Prose about the old approach is not the old approach; drop the
        # docstrings before looking, but keep every other string, because a
        # fabricated event hides in an ordinary JavaScript string literal.
        code = source
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    code = code.replace(doc, "")
        code = "\n".join(line for line in code.splitlines()
                          if not line.strip().startswith("#"))
        for marker in ("dispatchEvent", "new MouseEvent", "new KeyboardEvent",
                       "new Event(", "execCommand"):
            if marker in code:
                offenders.append(f"{path.name}: {marker}")
    assert not offenders, f"Fabricated input found: {offenders}"
