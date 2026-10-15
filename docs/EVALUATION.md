# Evaluation of the baseline agents on the demo slides

Generated on 2026-10-15 by `python -m slidetwin.eval` (code at the commit that adds this file).
Numbers and method only: the pictures (reading paths, coverage heatmaps, replay GIFs) stay in the
git-ignored `data/eval/` because the CAMELYON licence terms are unclear (DATA_LICENSES.md).

## Set-up

- **Slides.** The five CAMELYON16 demo slides of the manifest:
  - `c16_fa751c`: macro-metastasis, largest deposit 4.05 mm;
  - `c16_aba3ae`: micro-metastasis, 1.27 mm;
  - `c16_2abf4f`: micro-metastasis, 0.28 mm;
  - `c16_683d2d`: macro-metastasis, 4.74 mm;
  - `c16_f606e8`: negative.

  Sizes are from the annotations (largest contiguous deposit, Feret diameter; DECISIONS.md, M6).
- **Agents** (SPEC 12). Serpentine and random under both profiles; thumbnail-and-crop under
  `unconstrained` only. Viewport 1280 x 1024 px, random seed 0, protocol `sln_metastasis`
  (advisory).
- **Profiles.** `viewer`: preset magnifications, each action timed by the draft cost model.
  `unconstrained`: free crops, a flat 1 s per action, free thumbnail.
- **The LLM agent was not run.** It needs an API key, and this report should not depend on a paid
  model. Run it with `python -m slidetwin.agents llm ...` and evaluate its trajectory the same way.

## Metrics

| Metric | Meaning |
|---|---|
| virtual s | virtual time when the report was submitted |
| views | views and crops served |
| coverage | tissue seen at 10x or more, whole slide, from the receipt |
| violations | failed rule checks E1-E4 |
| conclusion correct | `present` on a tumour slide, `absent` on a negative one (`indeterminate` is not correct) |
| lesion seen at 20x | a view at 20x or more showed annotated tumour |
| findings on lesion | findings whose evidence views show annotated tumour |
| size error | reported size (largest cited measurement) minus the largest deposit, in mm (absolute) |

## Results

### Per agent and profile

Means over the five slides; counts over the readings that have a ground truth.

| agent | profile | readings | mean virtual s | mean views | mean coverage | violations | conclusion correct | lesion seen at 20x | findings on lesion | mean size error mm |
|---|---|---|---|---|---|---|---|---|---|---|
| crop | unconstrained | 5 | 6.0 | 7.0 | 1.6% | 0 | 4/5 | 1/4 | 5/24 | - |
| random | unconstrained | 5 | 20.0 | 21.0 | 0.7% | 10 | 1/5 | 1/4 | - | - |
| random | viewer | 5 | 15.0 | 21.0 | 0.7% | 10 | 1/5 | 1/4 | - | - |
| serpentine | unconstrained | 5 | 185.2 | 183.2 | 100.0% | 0 | 0/5 | 1/4 | 1/12 | 2.13 |
| serpentine | viewer | 5 | 132.0 | 183.2 | 100.0% | 0 | 0/5 | 1/4 | 1/12 | 2.13 |

### Per reading

| slide | agent | profile | virtual s | views | coverage | violations | conclusion | truth | category | lesion seen 10x/20x | lesion share 10x | time to lesion s | findings on lesion | size error mm |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| c16_2abf4f | crop | unconstrained | 6.0 | 7 | 1.0% | 0 | present | tumor | micro | no/no | 0% | - | 0 of 6 | - |
| c16_2abf4f | random | unconstrained | 20.0 | 21 | 0.5% | 2 | absent | tumor | none | no/no | 0% | - | 0 of 0 | - |
| c16_2abf4f | random | viewer | 15.0 | 21 | 0.5% | 2 | absent | tumor | none | no/no | 0% | - | 0 of 0 | - |
| c16_2abf4f | serpentine | unconstrained | 240.0 | 238 | 100.0% | 0 | indeterminate | tumor | none | yes/no | 100% | 111.0 | 0 of 3 | 0.36 |
| c16_2abf4f | serpentine | viewer | 167.5 | 238 | 100.0% | 0 | indeterminate | tumor | none | yes/no | 100% | 75.2 | 0 of 3 | 0.36 |
| c16_683d2d | crop | unconstrained | 6.0 | 7 | 1.3% | 0 | present | tumor | micro | no/no | 0% | - | 0 of 6 | - |
| c16_683d2d | random | unconstrained | 20.0 | 21 | 0.0% | 2 | absent | tumor | none | no/no | 0% | - | 0 of 0 | - |
| c16_683d2d | random | viewer | 15.0 | 21 | 0.0% | 2 | absent | tumor | none | no/no | 0% | - | 0 of 0 | - |
| c16_683d2d | serpentine | unconstrained | 144.0 | 142 | 100.0% | 0 | indeterminate | tumor | none | yes/no | 100% | 1.0 | 0 of 3 | 4.10 |
| c16_683d2d | serpentine | viewer | 103.8 | 142 | 100.0% | 0 | indeterminate | tumor | none | yes/no | 100% | 1.6 | 0 of 3 | 4.10 |
| c16_aba3ae | crop | unconstrained | 6.0 | 7 | 0.7% | 0 | present | tumor | micro | no/no | 0% | - | 0 of 6 | - |
| c16_aba3ae | random | unconstrained | 20.0 | 21 | 0.2% | 2 | absent | tumor | none | no/no | 0% | - | 0 of 0 | - |
| c16_aba3ae | random | viewer | 15.0 | 21 | 0.2% | 2 | absent | tumor | none | no/no | 0% | - | 0 of 0 | - |
| c16_aba3ae | serpentine | unconstrained | 271.0 | 269 | 100.0% | 0 | indeterminate | tumor | none | yes/no | 100% | 103.0 | 0 of 3 | 0.63 |
| c16_aba3ae | serpentine | viewer | 189.4 | 269 | 100.0% | 0 | indeterminate | tumor | none | yes/no | 100% | 69.9 | 0 of 3 | 0.63 |
| c16_f606e8 | crop | unconstrained | 6.0 | 7 | 4.0% | 0 | present | normal | micro | -/- | - | - | - of 6 | - |
| c16_f606e8 | random | unconstrained | 20.0 | 21 | 2.1% | 2 | absent | normal | none | -/- | - | - | - of 0 | - |
| c16_f606e8 | random | viewer | 14.8 | 21 | 2.1% | 2 | absent | normal | none | -/- | - | - | - of 0 | - |
| c16_f606e8 | serpentine | unconstrained | 68.0 | 66 | 100.0% | 0 | indeterminate | normal | none | -/- | - | - | - of 3 | - |
| c16_f606e8 | serpentine | viewer | 53.7 | 66 | 100.0% | 0 | indeterminate | normal | none | -/- | - | - | - of 3 | - |
| c16_fa751c | crop | unconstrained | 6.0 | 7 | 1.1% | 0 | present | tumor | micro | yes/yes | 5% | 1.0 | 5 of 6 | - |
| c16_fa751c | random | unconstrained | 20.0 | 21 | 0.4% | 2 | absent | tumor | none | yes/yes | 3% | 10.0 | 0 of 0 | - |
| c16_fa751c | random | viewer | 15.2 | 21 | 0.4% | 2 | absent | tumor | none | yes/yes | 3% | 7.9 | 0 of 0 | - |
| c16_fa751c | serpentine | unconstrained | 203.0 | 201 | 100.0% | 0 | indeterminate | tumor | none | yes/yes | 100% | 63.0 | 1 of 3 | 3.44 |
| c16_fa751c | serpentine | viewer | 145.8 | 201 | 100.0% | 0 | indeterminate | tumor | none | yes/yes | 100% | 43.6 | 1 of 3 | 3.44 |

## What the numbers show

- **Coverage separates the readers.** The serpentine agent saw all tissue at 10x on every slide
  (100.0%), and every negative claim it made passed E4. The crop agent saw 0.7-4.0% of the tissue
  and the random agent 0-2.1%.
- **A right answer is not a supported answer.** The crop agent called `present` on all five slides,
  so its conclusion was correct on the four tumour slides, and wrong on the negative one. Only 5 of
  its 24 findings showed annotated tumour, all on `c16_fa751c`. It broke no rule: its crops are
  sharp enough for E2, and it made no negative claim that E4 could test. The rule checks test
  whether evidence is adequate, not whether it shows the lesion; that needs the ground truth used
  here.
- **The colour heuristic finds lymphoid tissue, not metastases.** The serpentine agent's 20x checks
  showed tumour on 1 of 4 tumour slides (1 of 12 findings). Its size "measurements" (the visible
  extent of a purple region) are off by 0.36-4.10 mm. Its `indeterminate` conclusion is never
  counted as correct.
- **Virtual time depends on the profile, not only on the agent.** The same serpentine plan takes
  132 s on average under `viewer` (a pan costs about 0.65 s) and 185 s under `unconstrained`
  (1 s per action). The `viewer` cost model is a placeholder until it is calibrated.
- **Time to the lesion.** A full scan reached the first 10x view of annotated tumour after 1.6 s
  (`c16_683d2d`, whose tumour lies at the slide centre) to 75 s (`c16_2abf4f`, a 0.28 mm deposit).
  The random agent reached tumour only on `c16_fa751c`, the slide with the largest deposits.

## Limitations

- Five slides and deterministic baselines: the tables describe the environment and its checks, not
  the diagnostic ability of any method.
- The deposit and "shows tumour" definitions are drafts that need pathologist review.
- The cost model is a placeholder until it is calibrated on human readings (docs/PILOT.md).

## Reproduce

```bash
python -m slidetwin.eval convert --xml-dir /path/to/CAMELYON16/annotations
python -m slidetwin.eval run --out data/eval/RUN \
  --slides c16_fa751c,c16_aba3ae,c16_2abf4f,c16_f606e8,c16_683d2d --agents serpentine,random,crop
python -m slidetwin.eval evaluate --out data/eval/RUN
```

The run took about 10 minutes on one CPU node.
