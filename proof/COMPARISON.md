# Publisher comparison

Checked 2026-11-09 23:18 America/Chicago, after `python3 fetch_rates.py`.
Screenshots of that snapshot: `proof/widget_desktop.png` and `proof/widget_390.png`, taken 2026-11-10T05:18:26Z (2026-11-09 23:18:26 America/Chicago).
Widget values are `site/data/all.json`. File and page values were read again from the publishers, with a separate parse, after that run.

| Series | Widget value | Widget as-of | Publisher file | File as-of | Publisher page | Page as-of | Match |
| --- | --- | --- | --- | --- | --- | --- | --- |
| pmms30 | 7.18 | 2026-11-05 | 7.18 | 2026-11-05 | 7.18 | 2026-11-05 | YES |
| pmms15 | 6.57 | 2026-11-05 | 6.57 | 2026-11-05 | 6.57 | 2026-11-05 | YES |
| effr | 3.88 | 2026-11-06 | 3.88 | 2026-11-06 | 3.88 | 2026-11-06 | YES |
| treasury10y | 4.88 | 2026-11-09 | 4.88 | 2026-11-09 | 4.88 | 2026-11-09 | YES |
| fomc_target | 3.75–4.00 | 2026-10-28 | 3-3/4 to 4 percent | 2026-10-28 | same statement | 2026-10-28 | YES |

## Where each side was read

| Series | Widget | Publisher file | Publisher page |
| --- | --- | --- | --- |
| pmms30 / pmms15 | `site/data/all.json` | Last row of `PMMS_history.csv`: `11/5/2026,7.18,,6.57` | freddiemac.com/pmms: interestRate 7.18 and 6.57, "Average rates as of November 5, 2026" |
| effr | `site/data/all.json` | H.15 DDP CSV column `RIFSPFF_N.B`, last numeric row `2026-11-06,3.88` | H.15 release (release date November 9, 2026), Federal funds (effective), column Nov 6 = 3.88 |
| treasury10y | `site/data/all.json` | 2026 daily par yield CSV, newest row `11/09/2026` and column `10 Yr` = 4.88 (the `30 Yr` on that row is 5.26 and was not used) | Treasury TextView row `datetime="2026-11-09"` 10-year cell 4.88 |
| fomc_target | `site/data/all.json` | Statement `monetary20261028a.htm`: "target range for the federal funds rate at 3-3/4 to 4 percent" | Same statement |

Weekend and `ND` / `N/A` rows are not given a carried-forward value. The 10-year card is 4.88 as of Nov 9, not the prior 4.86 as of Nov 6.
