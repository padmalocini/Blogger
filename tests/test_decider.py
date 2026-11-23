from agent.decider import AlignmentDecider
from agent.enums import AlignmentCase, SearchIntent
from agent.models import ClassificationResult, IntentLabel, UserInput


def _label(text, intent, ambiguous=False, confidence="high"):
    return IntentLabel(text=text, intent=intent, ambiguous=ambiguous,
                       confidence=confidence, reasoning="test")


def test_aligned_intent_is_case_b():
    ui = UserInput(topic="t", initial_keywords=["k"], goal="g", format="f", tone="x")
    cls = ClassificationResult(
        topic=_label("t", SearchIntent.informational),
        keywords=[_label("k", SearchIntent.informational)],
    )
    assert AlignmentDecider().decide(ui, cls).case == AlignmentCase.b


def test_mismatch_is_case_c():
    ui = UserInput(topic="t", initial_keywords=["k"], goal="g", format="f", tone="x")
    cls = ClassificationResult(
        topic=_label("t", SearchIntent.informational),
        keywords=[_label("k", SearchIntent.transactional)],
    )
    assert AlignmentDecider().decide(ui, cls).case == AlignmentCase.c


def test_user_override_forces_case_b():
    ui = UserInput(topic="t", initial_keywords=["k"], goal="g", format="f",
                   tone="x", user_override=True)
    cls = ClassificationResult(
        topic=_label("t", SearchIntent.unclear),
        keywords=[_label("k", SearchIntent.unclear)],
    )
    assert AlignmentDecider().decide(ui, cls).case == AlignmentCase.b