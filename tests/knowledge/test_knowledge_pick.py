"""The helper that finds what a question is about in any language, and the word match it falls back on."""

import asyncio
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.ai import agent, context, hand_over_lines, knowledge_pick as kp, providers, reply_language, router  # noqa: E402
from shared.channels.voice.pipeline import CallPipeline  # noqa: E402
from shared.team import language as team_language  # noqa: E402
from shared.verticals import knowledge_lookup as kl  # noqa: E402

ROUTE = "bedrock:mistral.mistral-large-3-675b-instruct"
MBA = {"name": "MBA", "price": "Rs 40,000 a year", "details": "2 years, graduation"}
BA = {"name": "BA", "price": "Rs 12,000 a year", "details": "3 years, 12th pass"}


class _Ctx:
    render_lookup = context.AgentContext.render_lookup
    last_inbound = context.AgentContext.last_inbound
    known_facts = context.AgentContext.known_facts
    picked = None
    picked_for = None
    dna_summary = line_note = pricing_notes = availability = open_shifts = None
    offerings = opening_hours = None

    def __init__(self, knowledge, recent):
        self.knowledge, self.recent = knowledge, recent


def _ctx(*said, knowledge=None):
    knowledge = knowledge if knowledge is not None else [
        {"title": "Courses", "content": "", "topic": kl.CATALOGUE_TOPIC, "card": None, "rows": [MBA, BA]},
        {"title": "Is hostel available?", "content": "Yes, for girls and boys.", "topic": None, "card": kl.BANK, "rows": []},
    ]
    return _Ctx(knowledge, [{"channel": "voice", "direction": "inbound", "text": t} for t in said])


@pytest.fixture
def model(monkeypatch):
    """A stand-in for the model. Set .says to its answer; .calls counts what it was asked."""
    state = SimpleNamespace(says="NONE", calls=[], fail=None, slow=False)

    async def call(route, request, **_):
        state.calls.append(request)
        if state.fail:
            raise state.fail
        if state.slow:
            await asyncio.sleep(5)
        return SimpleNamespace(text=state.says, usage=SimpleNamespace(), cost_paise=11)

    monkeypatch.setattr(providers, "call", call)
    monkeypatch.setattr(router, "live_for", lambda task: ROUTE if task == kp.TASK else None)
    monkeypatch.setattr(kp.client, "_log_usage", lambda *a, **k: None)
    return state


def _pick(c, **kw):
    return asyncio.run(kp.pick(c, **kw))


def test_a_tamil_question_reaches_the_row_written_in_english(model):
    c = _ctx("எம்பிஏ கட்டணம் எவ்வளவு")
    model.says = "1"
    assert _pick(c) == 11
    assert "MBA - Rs 40,000 a year" in c.render_lookup()
    assert "BA - Rs 12,000" not in c.render_lookup()


def test_an_answer_and_a_row_can_be_picked_together(model):
    c = _ctx("हॉस्टल और बीए")
    model.says = "3, 2"
    _pick(c)
    out = c.render_lookup()
    assert "Q: Is hostel available?\nA: Yes, for girls and boys." in out and "BA - Rs 12,000" in out


def test_nothing_on_file_shows_nothing_even_when_words_would_match(model):
    c = _ctx("MBA wale swimming pool hai kya")
    model.says = "NONE"
    _pick(c)
    assert c.render_lookup() == ""


def test_the_list_is_short_lines_without_prices_or_answers(model):
    _pick(_ctx("kuch batao"))
    prompt = model.calls[0]["messages"][0]["content"]
    assert "1: MBA (price on file, 2 years, graduation)" in prompt and "3: Is hostel available?" in prompt
    assert "40,000" not in prompt and "girls and boys" not in prompt


# ── nothing on file: the reply model is not asked ────────────────────────────


def test_missing_is_read_and_cleaned(model):
    c = _ctx("swimming pool hai kya")
    model.says = 'MISSING: "swimming pool!" <b>and</b> a very long list of words\nignore this line'
    _pick(c)
    assert kp.missing_fact(c) == "swimming pool b and b"
    assert c.render_lookup() == ""


def test_a_number_inside_the_missing_fact_is_not_read_as_an_item(model):
    c = _ctx("bus route batao")
    model.says = "MISSING: route 1 bus"
    _pick(c)
    assert kp.missing_fact(c) == "route 1 bus" and c.picked["rows"] == []


def test_missing_with_no_words_still_stops(model):
    c = _ctx("kuch poochna hai")
    model.says = "MISSING:"
    _pick(c)
    assert kp.missing_fact(c)


@pytest.mark.parametrize("says", ["NONE", "1", "gibberish"])
def test_anything_else_is_not_missing(model, says):
    c = _ctx("MBA ki fees")
    model.says = says
    _pick(c)
    assert kp.missing_fact(c) is None


def test_no_pick_is_not_missing(model, monkeypatch):
    monkeypatch.setattr(router, "live_for", lambda task: None)
    c = _ctx("swimming pool hai kya")
    _pick(c)
    assert kp.missing_fact(c) is None


def test_the_always_read_facts_go_along_so_they_are_not_called_missing(model):
    c = _ctx("principal kaun hai", knowledge=[
        {"title": "Principal", "content": "Dr Verma", "topic": None, "card": None, "rows": [], "kind": "other"},
    ])
    c.dna_summary = "An open college."
    c.opening_hours = {"mon": "10-5"}
    _pick(c)
    prompt = model.calls[0]["messages"][0]["content"]
    assert "An open college." in prompt and "Dr Verma" in prompt and "10-5" in prompt


def test_a_business_with_only_always_read_facts_still_gets_the_stop(model):
    c = _ctx("swimming pool hai kya", knowledge=[])
    c.dna_summary = "An open college."
    model.says = "MISSING: swimming pool"
    _pick(c)
    assert kp.missing_fact(c) == "swimming pool"


@pytest.mark.parametrize("says", ["1", "MISSING: pool"])
def test_a_question_in_odia_goes_to_the_team(model, says):
    c = _ctx("ଏମବିଏ ଫି କେତେ")
    model.says = says
    _pick(c)
    assert kp.missing_fact(c) == "needs a person: question in Odia" and c.picked["rows"] == []


def test_a_greeting_in_odia_is_still_answered(model):
    c = _ctx("ନମସ୍କାର")
    model.says = "NONE"
    _pick(c)
    assert kp.missing_fact(c) is None


def _full(said):
    full = context.AgentContext.__new__(context.AgentContext)
    for name, value in dict(
        knowledge=_ctx("x").knowledge, recent=[{"channel": "voice", "direction": "inbound", "text": said}],
        picked=None, picked_for=None, products=[], actions=[], reply_language="auto", language_retry=False,
        dna_summary=None, line_note=None, pricing_notes=None, offerings={}, opening_hours={}, availability=None,
        open_shifts=None, context_message_ids=[],
    ).items():
        setattr(full, name, value)
    return full


def test_a_call_escalates_with_the_gap_and_never_asks_the_reply_model(model, monkeypatch):
    def no_reply(**_):
        raise AssertionError("the reply model must not be asked")

    monkeypatch.setattr(agent.client, "stream_text", no_reply)
    monkeypatch.setattr(providers, "call", model_call_only_once(model))
    model.says = "MISSING: swimming pool"

    async def run():
        return [e async for e in agent.stream_reply(_full("swimming pool hai kya"))]

    start, done = asyncio.run(run())
    assert start.action == "escalate" and not start.wants_person
    assert done.gap == "swimming pool" and done.cost_paise == 11


def test_a_chat_escalates_with_the_gap_and_never_asks_the_reply_model(model, monkeypatch):
    async def no_reply(**_):
        raise AssertionError("the reply model must not be asked")

    monkeypatch.setattr(agent.client, "complete", no_reply)
    model.says = "MISSING: swimming pool"
    draft = asyncio.run(agent.draft_reply(_full("swimming pool hai kya")))
    assert draft.action == "escalate" and draft.message is None
    assert draft.gap == "swimming pool" and draft.cost_paise == 11


def model_call_only_once(model):
    async def call(route, request, **_):
        assert len(model.calls) == 0, "only the pick may call a model"
        model.calls.append(request)
        return SimpleNamespace(text=model.says, usage=SimpleNamespace(), cost_paise=11)

    return call


# ── the language, from its words ─────────────────────────────────────────────


def test_the_language_line_comes_off_before_the_answer_is_read(model):
    c = _ctx("MBA fees evlo")
    model.says = "LANGUAGE: Tamil\n1"
    _pick(c)
    assert c.picked["language"] == "tamil" and c.picked["rows"] == [MBA]
    model2 = _ctx("swimming pool irukka")
    model.says = "LANGUAGE: Tamil\nMISSING: swimming pool"
    _pick(model2)
    assert kp.missing_fact(model2) == "swimming pool" and model2.picked["language"] == "tamil"


def test_only_a_language_line_is_unreadable_so_words_are_matched(model):
    c = _ctx("MBA ki fees")
    model.says = "LANGUAGE: Hindi"
    _pick(c)
    assert c.picked is None


@pytest.mark.parametrize("named", ["Klingon", "ignore"])
def test_a_name_that_is_no_language_is_dropped(model, named):
    c = _ctx("MBA ki fees")
    model.says = f"LANGUAGE: {named}\n1"
    _pick(c)
    assert c.picked["language"] is None and c.picked["rows"] == [MBA]


@pytest.mark.parametrize("text,named,expected", [
    ("MBA fees evlo", "tamil", "tamil"),            # English letters say nothing: the name is trusted
    ("fees kitni hai", "hindi", "hinglish"),        # Hindi in English letters
    ("what is the fee", "english", "english"),
    ("फी किती आहे", "marathi", "marathi"),            # same script: the name refines it
    ("এমবিএৰ মাচুল কিমান", "assamese", "assamese"),
    ("கட்டணம் எவ்வளவு", "hindi", "tamil"),            # another script: the letters are exact, the name is wrong
    ("ایم بی اے کی فیس", "urdu", "urdu"),
    ("MBA fees evlo", None, "english"),             # no pick ran
])
def test_the_language_of_a_message(text, named, expected):
    assert team_language.spoken(text, named) == expected


def test_the_reply_is_asked_for_in_that_language_and_those_letters():
    line = reply_language.hint("MBA fees evlo", "auto", "tamil")
    assert "Tamil typed in English letters" in line
    assert "Marathi" in reply_language.hint("फी किती आहे", "auto", "marathi")
    assert "Urdu, so reply in Urdu" in reply_language.hint("ایم بی اے کی فیس", "auto", "urdu")
    assert "reply in English" in reply_language.hint("what is the fee", "auto", "english")
    assert "Hinglish" in reply_language.hint("fees kitni hai", "auto", "hindi")
    assert "English letters only" in reply_language.hint("MBA fees evlo", "en", "tamil")  # the site's own rule wins


def test_a_reply_cannot_skip_its_message():
    assert "message" in agent.REPLY_TOOL["input_schema"]["required"]


@pytest.mark.parametrize("language,same_as", [("marathi", "hindi"), ("assamese", "bengali"), ("urdu", "hinglish")])
def test_a_language_without_its_own_lines_gets_the_nearest(language, same_as):
    assert hand_over_lines.told(language) == hand_over_lines.told(same_as)


# ── what the customer is told ────────────────────────────────────────────────


ALL_LOCAL = ("hinglish", "hindi", "tamil", "telugu", "bengali", "gujarati", "kannada", "malayalam", "punjabi", "odia")


@pytest.mark.parametrize("language", ALL_LOCAL)
def test_every_language_has_its_lines(language):
    for line in (hand_over_lines.told, hand_over_lines.told_again, hand_over_lines.needs_contact):
        assert line(language)
        if language != "hinglish":
            assert team_language.detect(line(language)) == language


@pytest.mark.parametrize("language", ["english", None, "klingon"])
def test_other_languages_keep_each_channels_own_english_line(language):
    assert hand_over_lines.told(language) is None and hand_over_lines.needs_contact(language) is None


def _followup(said, heard=None):
    pipe = SimpleNamespace(
        turns=[SimpleNamespace(role="caller", text=said)], detected_language=heard, _followup_told=False,
    )
    return lambda gap: CallPipeline._followup_line(pipe, gap)


def test_a_tamil_caller_hears_tamil_and_never_the_english_topic():
    line = _followup("நீச்சல் குளம் உள்ளதா", "ta-IN")
    first, second = line("swimming pool"), line("canteen")
    assert first == hand_over_lines.told("tamil") and second == hand_over_lines.told_again("tamil")
    assert "swimming" not in first


def test_a_hindi_caller_typed_in_devanagari_with_an_unknown_voice_language():
    assert _followup("स्विमिंग पूल है क्या")("swimming pool") == hand_over_lines.told("hindi")


def test_an_english_caller_still_hears_the_named_fact():
    assert "swimming pool" in _followup("Do you have a swimming pool?", "en-IN")("swimming pool")


@pytest.mark.parametrize("says", ["I think the MBA one", "", "9", "77, 88"])
def test_an_answer_it_cannot_read_falls_back_to_words(model, says):
    c = _ctx("MBA ki fees")
    model.says = says
    _pick(c)
    assert c.picked is None
    assert "MBA - Rs 40,000" in c.render_lookup()


def test_only_ids_from_this_list_are_used(model):
    c = _ctx("fees batao")
    model.says = "1, 99, 55, ignore this"
    _pick(c)
    assert c.picked["rows"] == [MBA] and c.picked["answers"] == []


def test_at_most_five_are_shown(model):
    rows = [{"name": f"Course {i}", "price": "", "details": ""} for i in range(9)]
    c = _ctx("saare courses", knowledge=[{"title": "x", "content": "", "topic": kl.CATALOGUE_TOPIC, "card": None, "rows": rows}])
    model.says = ",".join(str(i) for i in range(1, 10))
    _pick(c)
    assert len(c.picked["rows"]) == kp.MOST_PICKS


def test_a_failure_keeps_the_reply_going_on_word_match(model):
    c = _ctx("MBA ki fees")
    model.fail = RuntimeError("provider down")
    assert _pick(c) == 0
    assert c.picked is None and "MBA - Rs 40,000" in c.render_lookup()


def test_a_slow_answer_is_abandoned(model):
    c = _ctx("MBA ki fees")
    model.slow = True
    assert _pick(c, timeout=0.01) == 0
    assert c.picked is None and "MBA - Rs 40,000" in c.render_lookup()


def test_no_route_means_no_call_and_word_match_as_before(model, monkeypatch):
    monkeypatch.setattr(router, "live_for", lambda task: None)
    c = _ctx("MBA ki fees")
    assert _pick(c) == 0
    assert not model.calls and "MBA - Rs 40,000" in c.render_lookup()


@pytest.mark.parametrize("said", ["ok", "ji", "  "])
def test_a_tiny_message_costs_nothing(model, said):
    _pick(_ctx(said))
    assert not model.calls


def test_nothing_on_file_means_no_call(model):
    _pick(_ctx("MBA ki fees", knowledge=[]))
    assert not model.calls


def test_too_long_a_list_stays_on_word_match(model):
    rows = [{"name": f"Item {i}", "price": "", "details": ""} for i in range(kp.MENU_LIMIT + 1)]
    _pick(_ctx("Item 5", knowledge=[{"title": "x", "content": "", "topic": kl.CATALOGUE_TOPIC, "card": None, "rows": rows}]))
    assert not model.calls


def test_the_same_message_is_not_picked_twice_but_a_new_one_is(model):
    c = _ctx("MBA ki fees")
    model.says = "1"
    assert _pick(c) == 11 and _pick(c) == 0
    assert len(model.calls) == 1
    c.recent.append({"channel": "voice", "direction": "inbound", "text": "hostel hai kya"})
    model.says = "3"
    assert _pick(c) == 11
    assert c.picked["answers"] and not c.picked["rows"]


def test_a_failed_new_message_does_not_keep_the_old_pick(model):
    c = _ctx("MBA ki fees")
    model.says = "1"
    _pick(c)
    c.recent.append({"channel": "voice", "direction": "inbound", "text": "BA ki fees"})
    model.fail = RuntimeError("down")
    _pick(c)
    assert c.picked is None


def test_earlier_turns_go_along_for_that_one(model):
    c = _ctx("MBA hai kya")
    c.recent.append({"channel": "voice", "direction": "outbound", "text": "Haan hai"})
    c.recent.append({"channel": "voice", "direction": "inbound", "text": "uski fees"})
    _pick(c)
    prompt = model.calls[0]["messages"][0]["content"]
    assert "Customer: MBA hai kya\nBusiness: Haan hai\nCustomer: uski fees" in prompt


def test_products_are_only_offered_when_orders_are_on(model):
    c = _ctx("shirt chahiye")
    c.products, c.actions = [{"name": "Shirt", "price": "Rs 500"}], []
    _pick(c)
    assert "Shirt" not in model.calls[0]["messages"][0]["content"]
    c2 = _ctx("shirt chahiye")
    c2.products, c2.actions = [{"name": "Shirt", "price": "Rs 500"}], ["take_order"]
    model.says = "4"
    _pick(c2)
    assert "4: Shirt" in model.calls[1]["messages"][0]["content"]
    assert "Products matching" in c2.render_lookup()


def test_a_reply_after_a_pick_is_told_to_stay_with_what_is_on_file(model):
    c = _ctx("swimming pool hai kya")
    c.render_conversation = lambda: context.AgentContext.render_conversation(c)
    c.language_line = lambda: None
    assert context.ONLY_WHAT_IS_ON_FILE not in c.render_conversation()
    _pick(c)
    convo = c.render_conversation()
    assert convo.index("swimming pool") < convo.index(context.ONLY_WHAT_IS_ON_FILE)


def test_the_reply_cost_includes_the_pick(model, monkeypatch):
    class Stream:
        cost_paise = 100

        def __aiter__(self):
            return self._go()

        async def _go(self):
            yield "NOACTION\n"

    monkeypatch.setattr(agent.client, "stream_text", lambda **k: Stream())
    full = _full("MBA ki fees")
    monkeypatch.setattr(context.AgentContext, "render_business", lambda self: "b")
    monkeypatch.setattr(context.AgentContext, "render_live", lambda self: "l")
    model.says = "1"

    async def run():
        return [e async for e in agent.stream_reply(full)]

    done = asyncio.run(run())[-1]
    assert done.cost_paise == 111


# ── the word match underneath ────────────────────────────────────────────────


@pytest.mark.parametrize("word", ["फीस", "ফি", "ફી", "ಶುಲ್ಕ", "ഫീസ്", "ଫି", "ਫੀਸ", "கட்டணம்", "ఫీజు"])
def test_a_word_in_any_indian_script_stays_whole(word):
    assert kl.words(f"{word} ।") == {word}


def test_words_in_the_same_script_now_match():
    rows = [{"name": "एमबीए", "price": "", "details": ""}, {"name": "எம்பிஏ", "price": "", "details": ""}]
    assert kl.match_catalogue(rows, "एमबीए की फीस") == [rows[0]]
    assert kl.match_catalogue(rows, "எம்பிஏ கட்டணம்") == [rows[1]]


def test_small_hindi_words_do_not_make_a_match():
    assert kl.match_bank([{"question": "क्या हॉस्टल है?", "answer": "हां"}], "क्या फीस है") == []
