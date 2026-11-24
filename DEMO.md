# Demo video: runbook

This file is the runbook of the demo video of Lifeline for the Reactive Omni Buildathon. The video is shorter than 180 seconds: this is a rule of the buildathon. Each shot is a real surface of the product: the board at its public URL, `README.md` and `JUDGE.md` on github.com, the block explorers, and a terminal with a real command of this repository. The video file is not in the repository.

| File | Content |
|---|---|
| [`lifeline/demo.py`](lifeline/demo.py) | The capture step |
| [`evidence/demo/capture.json`](evidence/demo/capture.json) | The saved capture: the `requestId` and the four blocks |
| [`demo/script.md`](demo/script.md) | The shot list and the narration, with the voice, its settings and the spoken forms |
| [`demo/claims.md`](demo/claims.md) | The claims table: each number, hash, address, block and statement of the video with its source |
| [`demo/subtitles.srt`](demo/subtitles.srt) | The subtitles of the cut |
| [`demo/`](demo) | The scripts that build the video again from the footage |

## 1. Preconditions

- The live deployment of `deployments/manifest.json`, with the fleet enrolled (`python -m lifeline.ops fleet`).
- The experiment and the scenarios are done (`python -m lifeline.experiment fixtures`, `load`, `quarantine`, `junk`), and `JUDGE.md` is generated (`python -m lifeline.judge`).
- Two keys in the environment. `LIFELINE_FIXTURE_KEY` owns the fixtures: the capture needs it. `ELEVENLABS_API_KEY` is for the narration. No step of this file needs `LIFELINE_OWNER_KEY`.
- The quick start of [`README.md`](README.md#quick-start): Python, Foundry and `forge build`.
- For the footage and the cut: Google Chrome, `ffmpeg`, `xterm`, `Xvnc` or `Xvfb`, `xdotool`, the fonts Inter and JetBrains Mono, and two Python packages:

```
pip install playwright pillow
```

## 2. The capture step (no time limit)

```
python -m lifeline.demo capture [--rpc <chain>=<url> ...] [--timeout <seconds>] [--manifest <file>]
```

The command calls `poke()` on the fixture `armC.reactive` until the callback proxy of Base Sepolia emits `BlacklistContract(armC.callback)`. Then it waits for the rescue and prints each of the four steps with its block: the debt event, the hub's `Relayed` on Lasna with its `requestId`, the vault's `Result`, and `debt(armC.callback)` 0. On success it writes `evidence/demo/capture.json`. At the timeout (default 900 s) it exits with status 1, names the step that did not occur, and writes nothing.

- **It has no time limit and it measures nothing.** The network decides when the debt comes: one `poke()` is one callback, and the canary runs out after about 6 of them. The seconds of the rescue are in `JUDGE.md`, section "Rescue latency", from block timestamps.
- **Each capture is a new rescue.** Each of the two arm C contracts allows 4 rescues inside 3600 seconds; the next debt inside the window quarantines it. Run `python -m lifeline.ops status` before each capture, and leave time between two captures. The command sends no `poke()` while the next debt of one of the two contracts would quarantine it, or while the hub or the vault cannot serve. Do not release or enroll arm C again to get more captures.
- **It acts only on the two arm C fixtures of the manifest**, and it sends `poke()` and no other transaction.

Record the screen while the capture runs. The first command records the board; the second one types the capture command in a terminal with a large font and records that terminal at real speed:

```
python demo/board_live.py board-live &
LIFELINE_POLL_SECONDS=0.5 demo/record_terminal.sh capture "python -m lifeline.demo capture"
```

`LIFELINE_POLL_SECONDS` is the time between two reads of the chain (default 2 s). With 0.5 s a line of the output comes at most 0.5 s after its block; the value changes no block and no transaction. The recording is `demo/footage/capture.mkv`, with the time of each output line in `capture.timing`. The cut shows the part from the last `poke()` to `debt()` 0 at real speed with no cut; only the waits before it are cut.

Fork dry run of the capture, with no funds (the forks of the dry run of the README, with `forkrelay --watch` and the experiment fixtures): `python -m pytest lifeline/tests/test_demo_forks.py`, with the variables of [`README.md`](README.md#development). A dry run needs its own `--out` file.

## 3. Run the evidence generator again

```
python -m lifeline.judge
```

`evidence/judge/data.json` then has the captured loop: the entry of `rescues[]` with the `requestId` of `capture.json`, and its row in `latency.rescues[]`. Do not run `python -m lifeline.experiment measure` or `load` again: the capture sends events to arm C only, and a new measurement would give the three arms different loads.

Then correct the numbers of `README.md` that this run changed: the two counts of rescues in the table "Measured on testnet" of `## Proof` (from `latency.byChain.<chain>.rescues`). Check that each other number of the README is still equal to its file. Commit and push `JUDGE.md`, the evidence and the README before step 4, so that github.com shows them. The manifest does not change, so the board is not built again.

## 4. The footage

```
python demo/surfaces.py        # the board, README.md and JUDGE.md on github.com, the explorer pages
python demo/voice.py           # the narration, one clip for each entry of demo/narration.json
```

- `surfaces.py` opens each public page in Chrome, 1280 CSS pixels wide at 3 device pixels for one CSS pixel, and saves the page as a picture 3840 pixels wide: a zoom of the 1920x1080 cut stays sharp. It also saves the rectangle and the text of each element that the cut frames. Each URL comes from `capture.json`, `evidence/judge/data.json` and the manifest. It changes no element of a page. It closes the file tree of github.com, and it blocks the advertising hosts of Blockscout, as a browser with an ad blocker does.
- `voice.py` needs `ELEVENLABS_API_KEY`. It uses one voice and one setting for each clip, and it saves the time of each character: the subtitles take their times from it. Listen to each clip, or transcribe it and compare it with `demo/narration.json`; generate a clip again with `python demo/voice.py <clip id> --force` if a word, a number or a stress is wrong.
- The footage is in `demo/footage/`, which is not committed.

## 5. The shot list

The shots in order, with the surface, the on-screen action, the narration and the times: [`demo/script.md`](demo/script.md). Its sources are [`demo/shots.py`](demo/shots.py) (surfaces, camera, cursor, marks; each time is an anchor on a word of the narration) and [`demo/narration.json`](demo/narration.json) (text, voice, spoken forms).

The order of the narrative: the first line about the narration; the problem; what Lifeline is; set it once; a live rescue; the difference; when things break; guests; limits; where to check.

## 6. Build the video

```
python demo/compose.py         # demo/build/lifeline-demo.mp4, demo/subtitles.srt, demo/script.md
python demo/claims.py          # demo/claims.md; exit status 1 if one row is not equal
```

- `compose.py` uses no clock and no random value: the same footage gives the same cut. It refuses a cut of 180 seconds or more, a subtitle that is too long for one line, and a mark that is not on a still picture.
- What it adds on top of a surface, in the style of `board/template.html` (the dark tokens, Inter, JetBrains Mono): the subtitles, the address of the surface at the bottom left, a thin outline around one element, the label of the terminal recording ("waits cut", "real speed, no cut"), and the cursor (the standard arrow of Windows).
- `claims.py` builds each text of the table from the value at its source and looks for that text in the narration, in the terminal recording, or in the text of the framed element of the page. It reads the chain 5 times for the values that no file has (`--offline`: these rows are "not checked").
- `python demo/compose.py --frames 12.5,80` writes single pictures to `demo/build/frames/` for a check of one moment.

Checks before the upload:

```
ffprobe -v error -show_entries format=duration -of csv=p=0 demo/build/lifeline-demo.mp4      # less than 180
ffmpeg -i demo/build/lifeline-demo.mp4 -filter_complex "[0:v]setpts=PTS/2[v];[0:a]atempo=2[a]" \
  -map "[v]" -map "[a]" demo/build/lifeline-demo-2x.mp4                                       # the copy at double speed
```

Watch the copy at double speed one time: each sentence is understood, and each subtitle can be read. Give the video at 1.5x to a person who does not know the project, and ask: what does Lifeline do; what is different from a cron poller and from a wallet refiller; what does it not promise.

## 7. After this unit (operator)

1. Upload `demo/build/lifeline-demo.mp4` to a video host.
2. Put the link in `README.md`, in the line above the comment `<!-- DEMO VIDEO: … -->`, and remove the comment.
3. Submit on DoraHacks.
