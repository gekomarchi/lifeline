"""The shot list of the demo video: the order of the surfaces, the camera, the cursor and the marks.

`compose.py` reads this file. `demo/script.md` is written from it, so the two cannot differ.

Times are anchors on the narration, not seconds typed by hand:

    "n05"              the start of clip n05
    "n05$"             the end of clip n05
    "n05@Its gauge"    the start of these words in clip n05 (the text as sent to the voice engine)
    "...|-0.3"         0.3 s before that time

A region is a rectangle that `surfaces.py` read from the page: "<region>" of the surface of the shot, or
"<surface>:<region>" of another state of the same page.

A shot has:
    surface   the page capture or the terminal recording
    start     the anchor of its first frame; a shot ends where the next one starts
    views     the camera: [(anchor of the start of the move, seconds of the move, view)]; the first entry is
              the view at the start. A view is `fit([...regions])`, or `top(region, scale, dy)`.
    marks     [(from anchor, to anchor, region or regions)]: a thin outline around one element
    states    [(anchor, surface)]: the page changes its state at a click
    cursor    {"from": (x, y) on the screen, "moves": [(anchor of arrival, seconds, region, click or not)]}
    action    the text of the column "on-screen action" of the script
"""

from __future__ import annotations

# The pause before each clip, in seconds. A clip with no entry has DEFAULT_PAUSE.
LEAD_IN = 0.35
DEFAULT_PAUSE = 0.2
PAUSE = {"n02": 0.25, "n03": 0.35, "n05": 0.4, "n06": 0.4, "n08": 0.4, "n09": 0.25, "n10": 0.95, "n11": 0.4,
         "n12": 0.3, "n13": 0.5, "n14": 0.45, "n15": 0.3, "n16": 0.4, "n17": 0.3, "n18": 0.3, "n19": 0.35, "n20": 0.4,
         "n21": 0.4}
TAIL = 0.9  # the last picture stays for this time after the last word

PARTS = {1: "First line", 2: "The problem", 3: "What Lifeline is", 4: "Set it once", 5: "A live rescue",
         6: "The difference", 7: "When things break", 8: "Guests", 9: "Limits", 10: "Where to check"}


def fit(regions, pad=22, most=2.05, least=1.5):
    """The view that puts the regions in the part of the picture above the subtitles."""
    return {"fit": list(regions) if isinstance(regions, list) else [regions], "pad": pad, "most": most,
            "least": least}


def top(region, scale, dy=0.0):
    """The view with the top edge of the picture `dy` CSS pixels below the top of a region, at a fixed scale."""
    return {"top": region, "scale": scale, "dy": dy}


PAGE_TOP = {"at": (0, 0), "scale": 1.5}

SHOTS = [
    {"id": "s01", "part": 2, "surface": "reactscan-arm-a", "start": "0",
     "action": "The page of the arm A canary on Reactscan, from its top. Zoom in to the reserves and the debt of "
               "the contract. Zoom out to the list of its transactions; a mark on the status column, then on the "
               "counts of transactions and of reactive executions.",
     "views": [("0", 0, PAGE_TOP),
               ("n02|-0.15", 0.9, fit(["reserves", "debt"], pad=40, most=2.1)),
               ("n02@Each transaction|-0.6", 0.9, PAGE_TOP)],
     "marks": [("n02|+0.85", "n02@Each transaction|-0.7", "debt"),
               ("n02@Each transaction|+0.4", "n02@But the network|-0.15", ("status", {"h": 330})),
               ("n02@But the network|+0.05", "n02$|+0.05", "counts")]},

    {"id": "s02", "part": 2, "surface": "readme", "start": "n03|-0.3",
     "action": "README on github.com, section Problem. A mark on the line about the buildathon, then on the line "
               "about the builder's own automation.",
     "views": [("n03|-0.3", 0, fit("problem"))],
     "marks": [("n03|+0.15", "n03$|+0.2", "problemBuildathon"),
               ("n04|+0.05", "n04@Over|-0.4", "problemOwn")]},

    {"id": "s03", "part": 2, "surface": "board", "start": "n04@Over|-0.3",
     "action": "The board, panel Targets. A mark on the reserves and the marks of FastfillReactive.",
     "views": [("n04@Over|-0.3", 0, fit(["targetsHead", "row:FastfillExecutor"]))],
     "marks": [("n04@Over|+0.15", "n04$|+0.25", "reserves:FastfillReactive")]},

    {"id": "s04", "part": 3, "surface": "readme", "start": "n05|-0.3",
     "action": "README on github.com, the title and the first paragraph.",
     "views": [("n05|-0.3", 0, fit("top"))]},

    {"id": "s05", "part": 3, "surface": "readme", "start": "n05@Its airbag|-0.35",
     "action": "README, section How it works: the sequence diagram. First the airbag half; the picture then "
               "moves down to the gauge half.",
     "views": [("n05@Its airbag|-0.35", 0, top("diagram", 1.9, -16)),
               ("n05@Its gauge|-0.55", 0.9, top("diagram", 1.9, 128))]},

    {"id": "s06", "part": 4, "surface": "readme", "start": "n06|-0.3",
     "action": "README, section Operator guide: the `enroll` command with its policy flags.",
     "views": [("n06|-0.3", 0, fit(["enrollIntro", "enroll"]))]},

    {"id": "s07", "part": 4, "surface": "board", "start": "n07|-0.35",
     "action": "The board, panel Targets. A mark on the two fleet targets, FastfillReactive and FastfillExecutor, "
               "with their state and their marks.",
     "views": [("n07|-0.35", 0, fit(["targetsHead", "row:FastfillExecutor"]))],
     "marks": [("n07|+0.1", "n07@each with|-0.3", ["row:FastfillReactive", "row:FastfillExecutor"])]},

    {"id": "s08", "part": 4, "surface": "reactscan-enroll", "start": "n07@each with|-0.2",
     "action": "The `hub.enroll` transaction of FastfillExecutor on Reactscan. A mark on its input: the chain, the "
               "target and the policy.",
     "views": [("n07@each with|-0.2", 0, {"at": (0, 70), "scale": 1.5})],
     "marks": [("n07@each with|+0.5", "n07$|+0.3", "input")]},

    {"id": "s09", "part": 5, "surface": "terminal:capture", "start": "n08|-0.3",
     "action": "The terminal recording of `python -m lifeline.demo capture` on testnet. The command is typed. "
               "The waits between the `poke()` calls are cut (label: waits cut). From before the last `poke()` "
               "to `debt()` 0 the recording runs at real speed with no cut (label: real speed, no cut): the "
               "four steps appear with their block numbers. The screen then stays, and a mark goes to each "
               "step when the narration names it.",
     "terminal": {"typing": "n08|-0.3", "pokes": "n08@This command|+1.75", "real": "n09|-0.1",
                  "debtEventAt": "n09$|-0.6",
                  "marks": [("n10|+0.0", "n10@The hub|-0.12", "1 debt event"),
                            ("n10@The hub|+0.0", "n10@The vault|-0.12", "2 hub reaction"),
                            ("n10@The vault|+0.0", "n10@The debt is|-0.12", "3 vault callback"),
                            ("n10@The debt is|+0.0", "n10@Nobody|-0.1", "4 debt() is 0")]}},

    {"id": "s10", "part": 5, "surface": "reactscan-relayed", "start": "n11|-0.3",
     "action": "The `Relayed` transaction of the captured rescue on Reactscan: a mark on its block number. The "
               "picture moves down to its callback; a mark on the destination transaction.",
     "views": [("n11|-0.3", 0, {"at": (0, 70), "scale": 1.5}),
               ("n11@with its callback|-0.5", 0.9, fit(["callbacksTitle", "callbacks"]))],
     "marks": [("n11|+0.25", "n11@with its callback|-0.6", "block"),
               ("n11@with its callback|+0.5", "n11$|+0.3", "destination")]},

    {"id": "s11", "part": 5, "surface": "blockscout-debt", "start": "n12|-0.3",
     "action": "The transaction of the debt event on Blockscout (Base Sepolia). A mark on its timestamp.",
     "views": [("n12|-0.3", 0, fit(["title", "details"], pad=30, most=1.55))],
     "marks": [("n12|+0.35", "n12@The payment|-0.3", ["block", "timestamp"])]},

    {"id": "s12", "part": 5, "surface": "blockscout-result", "start": "n12@The payment|-0.2",
     "action": "The transaction of the vault `Result` on Blockscout, in the same framing. A mark on its timestamp.",
     "views": [("n12@The payment|-0.2", 0, fit(["title", "details"], pad=30, most=1.55))],
     "marks": [("n12@The payment|+0.3", "n12$|+0.6", ["block", "timestamp"])]},

    {"id": "s13", "part": 6, "surface": "readme", "start": "n13|-0.35",
     "action": "README, section Lifeline against the alternatives: the three-arm table, with a mark on the row "
               "of the delivered callbacks. The picture moves down to the notes (a mark on arm B), then to the "
               "sentence about the wallet refiller and the comparison table (a mark on the row \"Restarts a "
               "stopped reactive contract\").",
     "views": [("n13|-0.35", 0, fit(["armsIntro", "arms"], most=1.85)),
               ("n14|-0.4", 0.65, fit(["arms", "armsNotes"], most=1.85)),
               ("n15|-0.25", 0.7, fit(["notRefiller", "comparisonRestart"], most=1.85))],
     "marks": [("n13@With no guardian|-0.15", "n13$|+0.05", "armsCallbacks"),
               ("n14|+0.3", "n14$|+0.0", "armB"),
               ("n15@Lifeline pays|+0.0", "n15$|+0.3", "comparisonRestart")]},

    {"id": "s14", "part": 7, "surface": "board", "start": "n16|-0.35",
     "action": "The board, panel Targets. The cursor clicks the state filter Quarantined: one target stays, the "
               "looping canary. The cursor clicks Details: a mark on the quarantine rule, then on the state of "
               "the target.",
     "views": [("n16|-0.35", 0, top("targets", 1.5, -235))],
     "states": [("n16|+0.95", "board-quarantined"), ("n16@After two|+0.45", "board-quarantined-details")],
     "cursor": {"from": (1180, 760),
                "moves": [("n16|+0.7", 0.75, "filterQuarantined", True),
                          ("n16@After two|+0.2", 1.0, "board-quarantined:details:looping", True)]},
     "marks": [("n16@After two|+0.9", "n16@and paid|-0.2", "board-quarantined-details:rule"),
               ("n16@and paid|+0.0", "n16$|+0.3", "board-quarantined-details:state")]},

    {"id": "s15", "part": 7, "surface": "judge", "start": "n17|-0.3",
     "action": "JUDGE.md on github.com, section Junk flood. Marks: the count of failed callbacks, the reserves of "
               "the vault at the end, the self-care sum, the balance change that the payments do not explain.",
     "views": [("n17|-0.3", 0, top("junkFlood", 1.5, -14))],
     "marks": [("n17|+0.35", "n17@It lost|-0.2", "junkFailures"),
               ("n17@It lost|+0.1", "n17@and a little|-0.12", "junkReservesEnd"),
               ("n17@and a little|+0.0", "n17@Its other|-0.12", "junkSelfCare"),
               ("n17@Its other|+0.0", "n17$|+0.25", "junkUnexplained")]},

    {"id": "s16", "part": 7, "surface": "judge", "start": "n18|-0.3",
     "action": "JUDGE.md, section Junk recovery: the steps. Marks: the deposit and `VaultUp` of the owner, then "
               "the `Result` of the target that waited.",
     "views": [("n18|-0.3", 0, fit("junkRecoverySteps", most=1.7))],
     "marks": [("n18|+0.4", "n18@The hub|-0.25", ["junkDeposit", "junkVaultUp"]),
               ("n18@The hub|+0.1", "n18@twenty|-0.45", "junkResult")]},

    {"id": "s17", "part": 7, "surface": "judge", "start": "n18@twenty|-0.3",
     "action": "JUDGE.md, section Rescue latency: the last rows of the table of each rescue. A mark on the "
               "rescue that waited for the sweep: 127 blocks, 25 seconds. Each row above it is a rescue that a "
               "debt event started: 5 blocks, 1 second.",
     "views": [("n18@twenty|-0.3", 0, fit(("latencySweepRow", {"w": 1012, "up": 250}), most=1.7))],
     "marks": [("n18@twenty|+0.1", "n18$|+0.3", ("latencySweepRow", {"w": 1012}))]},

    {"id": "s18", "part": 8, "surface": "judge", "start": "n19|-0.3",
     "action": "JUDGE.md, section Guests.",
     "views": [("n19|-0.3", 0, fit("guests", most=1.9))]},

    {"id": "s19", "part": 9, "surface": "readme", "start": "n20|-0.3",
     "action": "README, section Limits: the first limit. The picture moves down to Not built and Demo versus "
               "production.",
     "views": [("n20|-0.3", 0, fit("limits", most=1.85)),
               ("n20@Testnet only|-0.6", 0.8, fit(["notBuilt", "demoProduction"], most=1.85))]},

    {"id": "s20", "part": 10, "surface": "readme", "start": "n21|-0.3",
     "action": "README on github.com, the title with the links to the live board and to JUDGE.md.",
     "views": [("n21|-0.3", 0, fit("top"))]},
]
