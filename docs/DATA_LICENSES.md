# Data sources and licenses

No slide, annotation, patient data or company data is committed to this repository.
This file records where each public slide comes from and under which terms we use it.

## OpenSlide test data (CI and unit tests)

| File | Source | License | sha256 |
|---|---|---|---|
| `Aperio/CMU-1-Small-Region.svs` (1.9 MB, 2220x2967 px, one level, 0.499 um/px) | https://openslide.cs.cmu.edu/download/openslide-testdata/Aperio/ | CC0 (as listed on the OpenSlide test-data index) | `ed92d5a9f2e86df67640d6f92ce3e231419ce127131697fbbce42ad5e002c8a7` |

`scripts/fetch_test_data.py` downloads it to `data/testdata/` and checks the hash. The file also contains
label and macro images; SlideTwin never serves associated images.

## CAMELYON16 (demo slides)

On 2026-10-16 the owner decided to serve these slides on the public Hugging Face Space
(DECISIONS.md, "Publication"), and the same day to demo them from a GitHub Codespace instead,
whose port the owner makes public for a demo. The reading UI shows each slide's source and licence
on the start screen.

- Source: the CAMELYON16 challenge, also mirrored in the AWS Open Data bucket `s3://camelyon-dataset/`.
- License statements differ between pages. The AWS registry and the CAMELYON17 data page say CC0, but the
  CAMELYON17 download page says CC BY-NC-ND 4.0.
- **Until the project owner confirms otherwise, we apply the stricter reading.**
  - Use only for non-commercial research demos.
  - Credit the source.
  - Do not modify image content. Resampled views for display are not edits of the dataset.
- Citations:
  - Ehteshami Bejnordi B, et al. JAMA. 2017;318(22):2199-2210.
  - Litjens G, et al. GigaScience. 2018;7(6):giy065.
- Demo slides (M10, `configs/slides/demo.yaml`, training set): `normal_108` (0.33 GB, the smallest
  negative slide) and `tumor_091` (0.55 GB, the smallest slide with a metastasis; its largest deposit
  is 1.40 mm, a micro-metastasis). Whole slides, since the stricter reading allows no cropping. The
  S3 copy of `tumor_091` was checked against the pinned hash on 2026-10-16.
- Evaluation slides (`configs/slides/manifest.yaml`, training set; label and lesion geometry agree):
  `tumor_082`, `tumor_093`, `tumor_081`, `normal_108`, `tumor_058`.
- Lesion annotations (ASAP XML) are used only by evaluation scripts and are never reachable through a
  session action.
