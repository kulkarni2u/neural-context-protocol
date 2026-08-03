"""Task definitions for the HotpotQA-style multi-hop benchmark.

Each task mimics one HotpotQA distractor-setting example: a question that
requires combining facts from two "gold" supporting paragraphs, buried among
several distractor paragraphs that share surface vocabulary but do not answer
the question — the same shape used in HotpotQA's own distractor split
(https://hotpotqa.github.io/), scaled down and reproduced with entirely
fictional entities.

This is NOT the official HotpotQA dataset. Fetching the real dataset
(hotpotqa.github.io / huggingface.co/datasets/hotpot_qa) and PlugMem's own
eval harness (github.com/TIMAN-group/PlugMem) both require network access
this environment's egress policy denied (huggingface.co, arxiv.org, and
cmu.edu are not on the allowlist). This benchmark is a same-shape synthetic
stand-in, following the exact convention already used by
``benchmarks/task_success`` and ``benchmarks/needle``: deterministic,
keyless, and reproducible in CI, with fictional entities so there is no
contamination risk with any real dataset or model training data.

Two HotpotQA question types are represented:

- ``bridge``: the question names entity A; gold paragraph 1 links A to a
  "bridge" entity B; gold paragraph 2 states the fact about B that actually
  answers the question. Paragraph 2 shares no vocabulary with the original
  question — only with paragraph 1 — which is what makes bridge questions
  hard for pure keyword/lexical retrieval.
- ``comparison``: the question names two entities directly; both gold
  paragraphs share vocabulary with the question, but the answer requires
  combining a fact from each (e.g. determining which of two came first).

Scoring is context adequacy, not model reasoning: success requires every
string in ``Task.must_include`` (case-insensitive) to appear somewhere in the
assembled context. This mirrors the mock-provider design in
``benchmarks/task_success`` — it measures whether the two facts needed to
answer survive into a budget-bounded context, not whether a model can
combine them correctly.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Paragraph:
    """One retrieved passage, mirroring a search/tool-result turn."""

    title: str
    text: str
    is_gold: bool = False

    @property
    def content(self) -> str:
        return f"{self.title}: {self.text}"


@dataclass(slots=True)
class Task:
    """One deterministic HotpotQA-style multi-hop scenario."""

    task_id: str
    kind: str  # "bridge" | "comparison"
    question: str
    answer: str
    paragraphs: list[Paragraph]
    must_include: list[str] = field(default_factory=list)

    @property
    def query_text(self) -> str:
        return self.question

    @property
    def gold_paragraphs(self) -> list[Paragraph]:
        return [p for p in self.paragraphs if p.is_gold]


_FILLER_TOPICS: tuple[str, ...] = (
    "quarterly maintenance schedule",
    "routine facilities inspection",
    "regional weather advisory",
    "unrelated municipal budget note",
    "generic supply-chain status update",
    "background sports league standings",
    "local transit timetable change",
    "general public-health bulletin",
    "unrelated agricultural yield report",
    "generic currency exchange note",
)


def _filler_paragraph(index: int) -> Paragraph:
    topic = _FILLER_TOPICS[index % len(_FILLER_TOPICS)]
    return Paragraph(
        f"Filler note {index + 1}",
        f"This is a {topic} filed under record {index + 1:03d}; it does not "
        "pertain to any entity in the question above and shares no answer-bearing fact.",
    )


def _with_filler(paragraphs: list[Paragraph], *, start: int) -> tuple[list[Paragraph], int]:
    """Interleave ``paragraphs`` with generic filler to bulk up raw context size.

    Real HotpotQA distractor contexts run ~10 paragraphs; a handful of
    hand-authored near-topic distractors alone keeps the raw, unbounded
    context too small for a matched token budget to meaningfully force
    selection. Filler paragraphs (topic-unrelated, no shared vocabulary with
    the question) bulk up volume the same way ``benchmarks/needle`` uses
    filler turns -- so a pure recency window has enough total content to
    plausibly drop an early gold paragraph, the failure mode this benchmark
    is built to surface.
    """

    out: list[Paragraph] = []
    idx = start
    for i, item in enumerate(paragraphs):
        out.append(item)
        # Two filler paragraphs after each real paragraph except the last.
        if i < len(paragraphs) - 1:
            out.append(_filler_paragraph(idx))
            idx += 1
            out.append(_filler_paragraph(idx))
            idx += 1
    return out, idx


def _bridge_task(
    task_id: str,
    *,
    question: str,
    answer: str,
    gold1: Paragraph,
    gold2: Paragraph,
    distractors: list[Paragraph],
    must_include: list[str],
) -> Task:
    gold1.is_gold = True
    gold2.is_gold = True
    # gold1 near the front, gold2 past the midpoint, both buried under filler
    # and near-topic distractors -- so a pure recency window's hit rate isn't
    # an artifact of gold-paragraph placement, and dropping gold1 (the one a
    # recency window is least likely to still hold) is the expected failure.
    core = [
        distractors[0],
        gold1,
        distractors[1],
        distractors[2],
        gold2,
        *distractors[3:],
    ]
    paragraphs, _ = _with_filler(core, start=0)
    return Task(
        task_id=task_id,
        kind="bridge",
        question=question,
        answer=answer,
        paragraphs=paragraphs,
        must_include=must_include,
    )


def _comparison_task(
    task_id: str,
    *,
    question: str,
    answer: str,
    gold1: Paragraph,
    gold2: Paragraph,
    distractors: list[Paragraph],
    must_include: list[str],
) -> Task:
    gold1.is_gold = True
    gold2.is_gold = True
    core = [
        distractors[0],
        distractors[1],
        gold1,
        distractors[2],
        distractors[3],
        gold2,
        *distractors[4:],
    ]
    paragraphs, _ = _with_filler(core, start=0)
    return Task(
        task_id=task_id,
        kind="comparison",
        question=question,
        answer=answer,
        paragraphs=paragraphs,
        must_include=must_include,
    )


def _all_tasks() -> list[Task]:
    tasks: list[Task] = []

    tasks.append(
        _bridge_task(
            "bridge_01_verolyn",
            question="What nation is the birthplace of the founder of Verolyn Dynamics?",
            answer="Ostravia",
            gold1=Paragraph(
                "Verolyn Dynamics",
                "Verolyn Dynamics was founded in 2011 by Talia Renn, a former propulsion engineer.",
            ),
            gold2=Paragraph(
                "Talia Renn",
                "Talia Renn was born in the coastal city of Marrowport, in the nation of Ostravia.",
            ),
            distractors=[
                Paragraph("Corvane Dynamics", "Corvane Dynamics was founded in 2009 by Piet Aldous, a former naval architect."),
                Paragraph("Fennwick Aerostructures", "Fennwick Aerostructures was founded in 2013 by Rina Solveig in Denmark."),
                Paragraph("Talia Renn (early career)", "Talia Renn worked as a propulsion intern at Corvane Dynamics before founding her own firm."),
                Paragraph("Marrowport", "Marrowport is a coastal port city known for its shipyards and fish markets."),
                Paragraph("Ostravia", "Ostravia is a small coastal nation bordering the Verolyn Sea, population 4.2 million."),
                Paragraph("Piet Aldous", "Piet Aldous was born in Rotterdam and studied naval architecture."),
            ],
            must_include=["talia renn", "ostravia"],
        )
    )

    tasks.append(
        _bridge_task(
            "bridge_02_kestrel",
            question="In what year did the university that trained the lead architect of the Kestrel Bridge open?",
            answer="1889",
            gold1=Paragraph(
                "Kestrel Bridge",
                "The Kestrel Bridge was designed by lead architect Doren Vashti, a graduate of Ambervale University.",
            ),
            gold2=Paragraph(
                "Ambervale University",
                "Ambervale University opened its doors in 1889 as the region's first engineering college.",
            ),
            distractors=[
                Paragraph("Halcyon Bridge", "The Halcyon Bridge was designed by architect Renn Oduya, a graduate of Thornfield Polytechnic."),
                Paragraph("Doren Vashti (career)", "Doren Vashti later consulted on three additional suspension bridge projects."),
                Paragraph("Thornfield Polytechnic", "Thornfield Polytechnic opened in 1904 and specializes in materials science."),
                Paragraph("Kestrel Bridge (construction)", "Construction of the Kestrel Bridge took six years and used a novel cable-stay design."),
                Paragraph("Ambervale University (present day)", "Ambervale University today enrolls roughly 18,000 students across five faculties."),
                Paragraph("Renn Oduya", "Renn Oduya graduated from Thornfield Polytechnic in 1998."),
            ],
            must_include=["ambervale university", "1889"],
        )
    )

    tasks.append(
        _bridge_task(
            "bridge_03_lumetra",
            question="What continent is home to the headquarters of the company that acquired Lumetra Robotics?",
            answer="South America",
            gold1=Paragraph(
                "Lumetra Robotics",
                "Lumetra Robotics was acquired in 2022 by Carrado Systems in an all-stock deal.",
            ),
            gold2=Paragraph(
                "Carrado Systems",
                "Carrado Systems is headquartered in Montevideo, on the continent of South America.",
            ),
            distractors=[
                Paragraph("Lumetra Robotics (products)", "Lumetra Robotics made warehouse picking arms before the acquisition."),
                Paragraph("Halvorsen Industrial", "Halvorsen Industrial acquired competitor Brannix Robotics in 2021."),
                Paragraph("Carrado Systems (founding)", "Carrado Systems was founded in 2005 as a logistics software vendor."),
                Paragraph("Montevideo", "Montevideo is the capital and largest city of Uruguay."),
                Paragraph("Brannix Robotics", "Brannix Robotics is based in Ontario, Canada."),
                Paragraph("Lumetra Robotics (staff)", "Most of Lumetra Robotics' engineering staff relocated after the acquisition."),
            ],
            must_include=["carrado systems", "south america"],
        )
    )

    tasks.append(
        _bridge_task(
            "bridge_04_orvane",
            question="What language is spoken in the region where the composer of the Orvane Symphony grew up?",
            answer="Occitan",
            gold1=Paragraph(
                "Orvane Symphony",
                "The Orvane Symphony was composed by Idalia Marchetti, who grew up in the Occitan-speaking Vasse Valley.",
            ),
            gold2=Paragraph(
                "Vasse Valley",
                "The Vasse Valley is a rural region where Occitan remains the dominant home language.",
            ),
            distractors=[
                Paragraph("Idalia Marchetti (training)", "Idalia Marchetti trained at the Verrance Conservatory before composing the symphony."),
                Paragraph("Bellonet Symphony", "The Bellonet Symphony was composed by Aurel Kastan, raised in Brittany."),
                Paragraph("Verrance Conservatory", "The Verrance Conservatory is known for its string program."),
                Paragraph("Orvane Symphony (premiere)", "The Orvane Symphony premiered in 1998 to mixed reviews."),
                Paragraph("Aurel Kastan", "Aurel Kastan grew up speaking Breton in coastal Brittany."),
                Paragraph("Vasse Valley (economy)", "The Vasse Valley economy relies on vineyards and dairy farming."),
            ],
            must_include=["vasse valley", "occitan"],
        )
    )

    tasks.append(
        _bridge_task(
            "bridge_05_halcyon",
            question="What is the maximum depth reached by the submersible operated by the crew that discovered the Halcyon Wreck?",
            answer="6,200 meters",
            gold1=Paragraph(
                "Halcyon Wreck",
                "The Halcyon Wreck was discovered by the Tarrow Deep Expedition crew using the submersible Nautide-7.",
            ),
            gold2=Paragraph(
                "Nautide-7",
                "The Nautide-7 submersible has a maximum operating depth of 6,200 meters.",
            ),
            distractors=[
                Paragraph("Halcyon Wreck (cargo)", "The Halcyon Wreck's cargo hold contained porcelain believed to be 300 years old."),
                Paragraph("Marrow Deep Expedition", "The Marrow Deep Expedition used the submersible Calder-3 in a separate 2019 survey."),
                Paragraph("Tarrow Deep Expedition (funding)", "The Tarrow Deep Expedition was funded by a consortium of maritime museums."),
                Paragraph("Calder-3", "The Calder-3 submersible has a maximum operating depth of 4,000 meters."),
                Paragraph("Nautide-7 (construction)", "The Nautide-7 was built in 2015 and refitted in 2020."),
                Paragraph("Halcyon Wreck (location)", "The Halcyon Wreck lies roughly 300 kilometers off the coast."),
            ],
            must_include=["nautide-7", "6,200 meters"],
        )
    )

    tasks.append(
        _bridge_task(
            "bridge_06_wrenfield",
            question="What material is used in the roof of the stadium named after the mayor who commissioned the Wrenfield transit line?",
            answer="titanium-zinc alloy panels",
            gold1=Paragraph(
                "Wrenfield transit line",
                "The Wrenfield transit line was commissioned by then-mayor Costa Belliveau in 2003.",
            ),
            gold2=Paragraph(
                "Belliveau Stadium",
                "Belliveau Stadium, named for the former mayor, has a roof built from titanium-zinc alloy panels.",
            ),
            distractors=[
                Paragraph("Wrenfield transit line (route)", "The Wrenfield transit line runs 14 stations across the northern district."),
                Paragraph("Sorel transit line", "The Sorel transit line was commissioned by mayor Odile Franck in 1998."),
                Paragraph("Costa Belliveau (biography)", "Costa Belliveau served two terms as mayor before retiring in 2011."),
                Paragraph("Franck Stadium", "Franck Stadium has a retractable fabric roof completed in 2002."),
                Paragraph("Belliveau Stadium (capacity)", "Belliveau Stadium seats approximately 42,000 spectators."),
                Paragraph("Wrenfield transit line (ridership)", "The Wrenfield transit line carries roughly 90,000 riders daily."),
            ],
            must_include=["belliveau stadium", "titanium-zinc alloy panels"],
        )
    )

    tasks.append(
        _bridge_task(
            "bridge_07_thessaly",
            question="What year was the guild founded that certifies the vintner who produces Thessaly Reserve?",
            answer="1742",
            gold1=Paragraph(
                "Thessaly Reserve",
                "Thessaly Reserve is produced by vintner Anouk Delacroix, certified by the Grand Vinter's Guild.",
            ),
            gold2=Paragraph(
                "Grand Vinter's Guild",
                "The Grand Vintner's Guild was founded in 1742 to standardize regional wine grading.",
            ),
            distractors=[
                Paragraph("Thessaly Reserve (tasting notes)", "Thessaly Reserve is noted for notes of blackcurrant and cedar."),
                Paragraph("Anouk Delacroix (career)", "Anouk Delacroix took over the family vineyard in 2004."),
                Paragraph("Coastal Vintner's Association", "The Coastal Vintner's Association was founded in 1810."),
                Paragraph("Grand Vintner's Guild (membership)", "The Grand Vintner's Guild has roughly 900 certified members today."),
                Paragraph("Thessaly Reserve (region)", "Thessaly Reserve grapes are grown on the Verrane slopes."),
                Paragraph("Anouk Delacroix (guild)", "Anouk Delacroix received Grand Vintner's Guild certification in 2011."),
            ],
            must_include=["grand vintner's guild", "1742"],
        )
    )

    tasks.append(
        _bridge_task(
            "bridge_08_orenthal",
            question="What is the top speed of the engine used in the aircraft designed by the winner of the Orenthal Prize?",
            answer="Mach 2.1",
            gold1=Paragraph(
                "Orenthal Prize",
                "The Orenthal Prize was won in 2015 by engineer Marisol Fenwick for her glider design.",
            ),
            gold2=Paragraph(
                "Fenwick glider engine",
                "Marisol Fenwick's later aircraft used the Corvax-9 engine, with a top speed of Mach 2.1.",
            ),
            distractors=[
                Paragraph("Orenthal Prize (history)", "The Orenthal Prize has been awarded annually since 1988."),
                Paragraph("Halvard Reyes", "Halvard Reyes won the Orenthal Prize in 2016 for a fixed-wing design."),
                Paragraph("Corvax-7 engine", "The Corvax-7 engine has a top speed of Mach 1.6."),
                Paragraph("Marisol Fenwick (early work)", "Marisol Fenwick began her career designing sailplanes."),
                Paragraph("Orenthal Prize (committee)", "The Orenthal Prize committee includes five aerospace engineers."),
                Paragraph("Fenwick glider (weight)", "The Fenwick glider weighs approximately 380 kilograms unloaded."),
            ],
            must_include=["corvax-9", "mach 2.1"],
        )
    )

    tasks.append(
        _comparison_task(
            "comparison_01_rovers",
            question="Which of the two rovers, Kestrel-9 or Halyard-2, completed its primary mission first, and in what year?",
            answer="Kestrel-9, 2027",
            gold1=Paragraph(
                "Kestrel-9",
                "The Kestrel-9 rover completed its primary mission in 2027 after 400 sols on Mars.",
            ),
            gold2=Paragraph(
                "Halyard-2",
                "The Halyard-2 rover completed its primary mission in 2031 after 620 sols on Mars.",
            ),
            distractors=[
                Paragraph("Kestrel-9 (landing)", "The Kestrel-9 rover landed in the Vasitra crater in early 2026."),
                Paragraph("Halyard-2 (landing)", "The Halyard-2 rover landed in the Corrin basin in mid-2029."),
                Paragraph("Kestrel-8", "The Kestrel-8 rover, an earlier model, completed its mission in 2019."),
                Paragraph("Halyard-1", "The Halyard-1 rover was lost during descent in 2024."),
                Paragraph("Kestrel-9 (instruments)", "The Kestrel-9 rover carried a spectrometer and drill assembly."),
                Paragraph("Halyard-2 (instruments)", "The Halyard-2 rover carried a ground-penetrating radar."),
            ],
            must_include=["kestrel-9", "2027", "halyard-2", "2031"],
        )
    )

    tasks.append(
        _comparison_task(
            "comparison_02_towers",
            question="Which of the two towers, Solmere Spire or Draven Tower, is taller?",
            answer="Solmere Spire",
            gold1=Paragraph(
                "Solmere Spire",
                "Solmere Spire stands 412 meters tall, completed in 2019.",
            ),
            gold2=Paragraph(
                "Draven Tower",
                "Draven Tower stands 378 meters tall, completed in 2015.",
            ),
            distractors=[
                Paragraph("Solmere Spire (architect)", "Solmere Spire was designed by the firm Rennhart & Boye."),
                Paragraph("Draven Tower (architect)", "Draven Tower was designed by architect Yusuf Karam."),
                Paragraph("Corrin Spire", "Corrin Spire stands 290 meters tall, completed in 2008."),
                Paragraph("Draven Tower (tenants)", "Draven Tower houses primarily financial services firms."),
                Paragraph("Solmere Spire (observation deck)", "Solmere Spire's observation deck is on the 88th floor."),
                Paragraph("Rennhart & Boye", "Rennhart & Boye is a firm based in Zurich."),
            ],
            must_include=["solmere spire", "412 meters", "draven tower", "378 meters"],
        )
    )

    tasks.append(
        _comparison_task(
            "comparison_03_novels",
            question="Which novel was published first, The Ashgrove Ledger or The Corrin Accord?",
            answer="The Ashgrove Ledger",
            gold1=Paragraph(
                "The Ashgrove Ledger",
                "The Ashgrove Ledger was published in 1987 by author Renata Voss.",
            ),
            gold2=Paragraph(
                "The Corrin Accord",
                "The Corrin Accord was published in 1994 by author Bram Ostley.",
            ),
            distractors=[
                Paragraph("The Ashgrove Ledger (reception)", "The Ashgrove Ledger won a regional literary prize in 1988."),
                Paragraph("The Corrin Accord (reception)", "The Corrin Accord was adapted into a film in 2001."),
                Paragraph("Renata Voss", "Renata Voss also wrote three short-story collections."),
                Paragraph("Bram Ostley", "Bram Ostley taught creative writing before publishing novels."),
                Paragraph("The Wrenfield Papers", "The Wrenfield Papers was published in 1979 by a different author."),
                Paragraph("The Corrin Accord (sequel)", "A sequel to The Corrin Accord was published in 1999."),
            ],
            must_include=["the ashgrove ledger", "1987", "the corrin accord", "1994"],
        )
    )

    tasks.append(
        _comparison_task(
            "comparison_04_reactors",
            question="Which reactor design, Solvane-C or Thane-B, has the higher rated output?",
            answer="Solvane-C",
            gold1=Paragraph(
                "Solvane-C",
                "The Solvane-C reactor design is rated at 1,340 megawatts.",
            ),
            gold2=Paragraph(
                "Thane-B",
                "The Thane-B reactor design is rated at 980 megawatts.",
            ),
            distractors=[
                Paragraph("Solvane-C (safety)", "The Solvane-C design uses a passive cooling loop."),
                Paragraph("Thane-B (safety)", "The Thane-B design uses an active dual-pump cooling system."),
                Paragraph("Solvane-B", "The earlier Solvane-B design was rated at 1,100 megawatts."),
                Paragraph("Thane-A", "The Thane-A design, since retired, was rated at 640 megawatts."),
                Paragraph("Solvane-C (deployment)", "Solvane-C reactors are deployed at three sites currently."),
                Paragraph("Thane-B (deployment)", "Thane-B reactors are deployed at five sites currently."),
            ],
            must_include=["solvane-c", "1,340 megawatts", "thane-b", "980 megawatts"],
        )
    )

    tasks.append(
        _comparison_task(
            "comparison_05_glaciers",
            question="Which glacier, Verrane or Corstal, retreated more between 2000 and 2020?",
            answer="Corstal",
            gold1=Paragraph(
                "Verrane Glacier",
                "The Verrane Glacier retreated 1.2 kilometers between 2000 and 2020.",
            ),
            gold2=Paragraph(
                "Corstal Glacier",
                "The Corstal Glacier retreated 3.4 kilometers between 2000 and 2020.",
            ),
            distractors=[
                Paragraph("Verrane Glacier (location)", "The Verrane Glacier sits in the northern Ambervale range."),
                Paragraph("Corstal Glacier (location)", "The Corstal Glacier sits in the eastern Thornfield range."),
                Paragraph("Halyard Glacier", "The Halyard Glacier retreated 0.6 kilometers over the same period."),
                Paragraph("Verrane Glacier (monitoring)", "The Verrane Glacier has been monitored by satellite since 1995."),
                Paragraph("Corstal Glacier (monitoring)", "The Corstal Glacier has been monitored by ground survey since 1998."),
                Paragraph("Ambervale range", "The Ambervale range hosts several smaller ice fields."),
            ],
            must_include=["verrane glacier", "1.2 kilometers", "corstal glacier", "3.4 kilometers"],
        )
    )

    tasks.append(
        _comparison_task(
            "comparison_06_orchestras",
            question="Which orchestra, the Marrowport Philharmonic or the Ostravia Symphony, was founded earlier?",
            answer="Ostravia Symphony",
            gold1=Paragraph(
                "Marrowport Philharmonic",
                "The Marrowport Philharmonic was founded in 1921.",
            ),
            gold2=Paragraph(
                "Ostravia Symphony",
                "The Ostravia Symphony was founded in 1889.",
            ),
            distractors=[
                Paragraph("Marrowport Philharmonic (conductor)", "The Marrowport Philharmonic's current conductor took the post in 2018."),
                Paragraph("Ostravia Symphony (conductor)", "The Ostravia Symphony's current conductor took the post in 2015."),
                Paragraph("Thornfield Chamber Orchestra", "The Thornfield Chamber Orchestra was founded in 1955."),
                Paragraph("Marrowport Philharmonic (venue)", "The Marrowport Philharmonic performs at Corrin Hall."),
                Paragraph("Ostravia Symphony (venue)", "The Ostravia Symphony performs at Vasse Hall."),
                Paragraph("Corrin Hall", "Corrin Hall seats 1,800 concertgoers."),
            ],
            must_include=["marrowport philharmonic", "1921", "ostravia symphony", "1889"],
        )
    )

    tasks.append(
        _comparison_task(
            "comparison_07_bridges",
            question="Which bridge, the Kestrel Bridge or the Halcyon Bridge, has the longer main span?",
            answer="Kestrel Bridge",
            gold1=Paragraph(
                "Kestrel Bridge span",
                "The Kestrel Bridge has a main span of 1,280 meters.",
            ),
            gold2=Paragraph(
                "Halcyon Bridge span",
                "The Halcyon Bridge has a main span of 960 meters.",
            ),
            distractors=[
                Paragraph("Kestrel Bridge (traffic)", "The Kestrel Bridge carries roughly 60,000 vehicles daily."),
                Paragraph("Halcyon Bridge (traffic)", "The Halcyon Bridge carries roughly 45,000 vehicles daily."),
                Paragraph("Sorel Bridge", "The Sorel Bridge has a main span of 700 meters."),
                Paragraph("Kestrel Bridge (opening)", "The Kestrel Bridge opened to traffic in 1994."),
                Paragraph("Halcyon Bridge (opening)", "The Halcyon Bridge opened to traffic in 2001."),
                Paragraph("Sorel Bridge (opening)", "The Sorel Bridge opened to traffic in 1978."),
            ],
            must_include=["kestrel bridge", "1,280 meters", "halcyon bridge", "960 meters"],
        )
    )

    return tasks


TASKS: list[Task] = _all_tasks()


def get_tasks(limit: int | None = None) -> list[Task]:
    """Return the task list, optionally truncated to the first ``limit`` tasks."""

    if limit is None:
        return list(TASKS)
    if limit < 1:
        raise ValueError("limit must be >= 1")
    return list(TASKS[:limit])


def score_context(context: str, task: Task) -> tuple[bool, list[str]]:
    """Context-adequacy check: do all of ``task.must_include`` survive?

    Returns ``(success, missing)`` where ``missing`` lists the substrings
    (lowercased) that did not survive into ``context``. This checks whether
    the facts needed to answer are present, not whether a model combines
    them correctly -- the same "context adequacy at a matched budget" framing
    ``benchmarks/task_success`` already uses for its mock provider.
    """

    lower = context.lower()
    missing = [needle for needle in task.must_include if needle not in lower]
    return not missing, missing
