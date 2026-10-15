# Pathologist pilot: guide for the study team

SlideTwin is a research prototype, not for clinical use. This guide is for running a small pilot in
which pathologists read slides in the web UI. The participants' own instructions are in the UI, in
English and Chinese (the Help button).

## Aims

1. Check that reading in SlideTwin feels close enough to a real viewer to be worth measuring.
2. Collect human trajectories to calibrate the cost model (SPEC 8).
3. Find missing features and wrong rules (the protocol's draft rules need pathologist review).

## Setup

- **Where.**
  - Public demo slides only: the Hugging Face Space, or `docker run -p 7860:7860 slidetwin`.
  - Internal slides only on a machine inside the institution (`docker run ... -v /path:/data/slides`
    with your own manifest), never on the Space.
- **Slides.** Choose 3-5 slides per participant from the manifest. Mix positive (macro and micro)
  and negative slides, and vary the order between participants.
- **Rules.** Keep the `viewer` profile and the `sln_metastasis` protocol. Leave the timer hidden
  (the start screen's default) unless the study asks for it.
- **Pseudonyms.** Give each participant a pseudonym such as `p01`; record the link to the person
  outside SlideTwin. Never use names.
- **Logs.** The server writes every trajectory to `SLIDETWIN_LOG_DIR/sessions/` as it happens, and
  feedback to `SLIDETWIN_LOG_DIR/feedback/`. On the Space these folders are lost on restart, so
  participants also download their trajectory.

## Session plan (about 45 minutes)

1. **Introduction (5 min).** Explain the purpose and that this is not a diagnostic tool. Show the
   Help dialog.
2. **Practice (5 min).** One reading on `cmu1_small`; it is not analysed.
3. **Readings (30 min).** One session per slide. Ask participants to read as they would at work and
   to fill in the report, including a size where they would give one.
4. **Feedback (5 min).** The optional form below the receipt (three ratings and a comment), then a
   short conversation using the questions below.

## What to collect

- The trajectories (`*.jsonl`) and the feedback files (`*.json`), named by session id.
- Notes from the conversation, with the pseudonym and the session ids.

Questions for the conversation:

- Which actions felt slower or faster than at your viewer?
- Did the magnification presets and keys feel natural? Which keys would you change?
- Did anything on the screen distract from reading?
- Were the report fields enough for this task? Which rule checks were surprising?

## After the pilot

- **Cost model.** Fit it from the human trajectories, then review the result before changing
  `configs/profiles/viewer.yaml`:

  ```bash
  python scripts/calibrate_costs.py /path/to/logs/sessions --fitted fitted.yaml
  ```

  The script uses the time between a participant's actions, leaves out breaks longer than 60 s, and
  prints each parameter with its number of observations and a 95% interval.
- **Protocol.** Record the pathologists' corrections to the draft rules in `docs/DECISIONS.md`
  ("Needs pathologist review") and in the protocol YAML (`status`, `reviewed_by`, `reviewed_on`).
- **Data.** Trajectories from internal slides stay inside the institution.

## Known limitations to tell participants

- Views appear one at a time: dragging moves the image only when you release, and there is no
  continuous zoom.
- Only preset magnifications are offered; the highest one available depends on the scan.
- A server restart ends the session (download the trajectory at the end).
