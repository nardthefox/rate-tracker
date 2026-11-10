# Rate tracker

Public-data mortgage and Fed rate widget for [toddrenard.com](https://www.toddrenard.com). The page is a static site for GitHub Pages and can be embedded in an iframe.

National averages for information only. Not a rate quote, offer, or commitment to lend. Todd Renard is a real estate agent, not a mortgage lender or loan originator. Harbor Realty & Management Group.

## Sources

Each series is fetched directly from the publisher. No API key.

| Series | Publisher |
| --- | --- |
| 30-year and 15-year fixed averages | [Freddie Mac PMMS history CSV](https://www.freddiemac.com/pmms/docs/PMMS_history.csv) |
| Effective federal funds rate | [Federal Reserve H.15 Data Download Program](https://www.federalreserve.gov/datadownload/Output.aspx?rel=H15&series=646250c87b1afd04cc6774796fc0cec8&lastobs=280&filetype=csv&label=include&layout=seriescolumn&type=package), series `RIFSPFF_N.B` |
| FOMC target range | Latest FOMC statement linked from the [FOMC calendar](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) |
| 10-year Treasury yield | [U.S. Treasury daily par yield curve CSV](https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/2026/all?_format=csv&type=daily_treasury_yield_curve) |

FRED, Optimal Blue / OBMMI, MBA, and Fannie Mae are not used.

Figures are the publisher's prints. A blank, `ND`, or `N/A` stays blank. Nothing is interpolated or copied onto a later date. If a request fails, that series keeps the last stored good value only when the value still has its real as-of date. Otherwise the card says Unavailable. One publisher failing leaves the other series up.

Every figure on the page shows its source and as-of date. The page has no analytics and no third-party scripts.

## Local preview

```bash
python3 -m unittest -v
python3 fetch_rates.py
python3 -m http.server 8877 --directory site
```

Open `http://127.0.0.1:8877/`. `fetch_rates.py` writes `site/data/*.json` and `site/data/all.js`. Raw publisher downloads stay in `raw/` and are not published.

## GitHub Pages

`.github/workflows/update-rates.yml` runs the fetch on weekdays (three UTC passes: 14:15, 17:45, and 22:20) and from **Run workflow**. It commits only the public files in `site/data`, then deploys the `site/` directory with `actions/upload-pages-artifact` and `actions/deploy-pages`.

The workflow does not enable Pages and does not create secrets.

After this change is merged, the one-time setup is:

1. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
2. **Actions → Update rates → Run workflow** on `main` (or wait for the next weekday run).
3. The site is [https://nardthefox.github.io/rate-tracker/](https://nardthefox.github.io/rate-tracker/).

Embed:

```html
<iframe title="Rates at a glance" src="https://nardthefox.github.io/rate-tracker/" width="100%" height="780" style="border:0"></iframe>
```

The page is built for a desktop width and for 390px.

## Known limit

The H.15 release page says the Data Download Program's package builder is being retired. The CSV URL above still returns series `RIFSPFF_N.B`. If that URL stops answering, the effective funds rate stays on its last good value and as-of date. This project does not switch that series to FRED.
