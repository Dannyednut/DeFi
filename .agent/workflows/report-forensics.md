---
description: Generate comprehensive forensic reports and visualizations from logs.
---

1. Ensure the logs directory contains `.jsonl` files (lifecycle, pending_opportunities, shadow_trades, etc.).
2. Run the reporter module to process logs and generate reports:
// turbo
```powershell
python reporter.py
```
3. Check the generated text report in `logs/reports/`.
4. View the visualizations in `logs/reports/visualizations/`.
5. (Optional) Export logs to CSV for deeper analysis:
// turbo
```powershell
python -c "from reporter import ResearchReporter; ResearchReporter().export_all_to_csv()"
```
