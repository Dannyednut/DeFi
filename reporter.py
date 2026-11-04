"""
Research Reporter Module
========================
Forensics and analytical reporting for arbitrage opportunities.
Scans JSONL logs and generates research-ready summaries and CSVs.
"""
from __future__ import annotations

import json
import os
import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime
from pathlib import Path
from typing import Optional

from log import get_logger
from config import LOG_DIR

log = get_logger("reporter")

class ResearchReporter:
    def __init__(self, log_dir: str = LOG_DIR):
        self.log_dir = Path(log_dir)
        self.report_dir = self.log_dir / "reports"
        self.report_dir.mkdir(exist_ok=True)
        self.viz_dir = self.report_dir / "visualizations"
        self.viz_dir.mkdir(exist_ok=True)

    def generate_full_analysis(self) -> str:
        """Runs all forensic reports and returns a summary string."""
        log.info("Starting forensic analysis of logs...")
        
        results = [
            "=== DeFi Research Tool: Forensic Report ===",
            f"Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "\n"
        ]

        # 1. Opportunity Summary
        opp_stats = self._analyze_opportunity_distribution()
        results.append(opp_stats)

        # 2. Simulation vs Reality (Accuracy)
        accuracy_stats = self._analyze_simulation_accuracy()
        results.append(accuracy_stats)

        # 3. Competition & Shadow Trades
        competition_stats = self._analyze_competition()
        results.append(competition_stats)
        
        # 4. Generate Visualizations (PnL Curves)
        viz_status = self._generate_visualizations()
        results.append(f"\n--- Visualizations ---\n{viz_status}")

        final_report = "\n".join(results)
        
        # Save to file
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        report_path = self.report_dir / f"forensic_report_{timestamp}.txt"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(final_report)
            
        log.info(f"Report generated: {report_path}")
        return final_report

    def export_all_to_csv(self):
        """Converts all JSONL logs to CSV for external research (Pandas/Excel)."""
        log.info("Exporting logs to CSV...")
        jsonl_files = list(self.log_dir.glob("*.jsonl"))
        
        for file in jsonl_files:
            try:
                # Read JSONL
                data = []
                with open(file, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            line_data = json.loads(line)
                            data.append(line_data)
                        except:
                            continue
                
                if not data:
                    continue
                
                # Convert to DataFrame and save
                df = pd.DataFrame(data)
                csv_path = self.report_dir / f"{file.stem}.csv"
                df.to_csv(csv_path, index=False)
                log.debug(f"Exported {file.name} -> {csv_path.name}")
            except Exception as e:
                log.error(f"Failed to export {file.name}: {e}")

    # ── Forensic Implementation ───────────────────────────────────────────────

    def _analyze_opportunity_distribution(self) -> str:
        """Aggregates occurrence and profit by Type and DEX."""
        opp_file = self.log_dir / "lifecycle.jsonl"
        if not opp_file.exists():
            return "No lifecycle logs found."

        try:
            df = pd.read_json(opp_file, lines=True)
            if df.empty:
                return "Lifecycle logs are empty."

            # In the consolidated format, every line represents a unique opportunity
            # It may be 'OPEN' or 'CLOSED', but both have peak_profit and open_time
            if df.empty:
                return "Lifecycle logs are empty."

            # SANITY CHECK: Cap profit at $1M for stats to avoid skewing from simulation bugs
            PROFIT_CAP = 1_000_000.0
            df['peak_profit_usd'] = df['peak_profit_usd'].clip(upper=PROFIT_CAP)

            groupby_cols = ['opp_type', 'iddfs_depth'] if 'iddfs_depth' in df.columns else 'opp_type'
            summary = df.groupby(groupby_cols).agg({
                'fingerprint': 'count',
                'peak_profit_usd': ['sum', 'mean', 'max'],
                'duration_seconds': 'mean'
            })
            
            summary.columns = ['Count', 'Total Potential Profit ($)', 'Avg Profit ($)', 'Max Profit ($)', 'Avg Lifetime (s)']
            return "--- Opportunity Distribution (Type & IDDFS Depth) ---\n" + summary.to_string() + "\n"
        except Exception as e:
            return f"Error analyzing distribution: {e}"

    def _analyze_simulation_accuracy(self) -> str:
        """Correlates Simulation (Pending) with Reality (Outcome)."""
        pending_file = self.log_dir / "pending_opportunities.jsonl"
        if not pending_file.exists():
            return "No pending logs found for accuracy check."

        try:
            # Use chunks or handles to avoid "Value is too big" if one line is corrupted
            data = []
            with open(pending_file, "r") as f:
                for line in f:
                    try:
                        data.append(json.loads(line))
                    except:
                        continue
            
            df = pd.DataFrame(data)
            if df.empty or 'tx_outcome' not in df.columns:
                return "Outcome data (tx_outcome) not yet resolved in logs."
            
            mined = df[df['tx_outcome'] == 'mined'].copy()
            if mined.empty:
                return "No mined transactions found to check accuracy."

            # Clean extreme values
            mined['estimated_profit_usd'] = pd.to_numeric(mined['estimated_profit_usd'], errors='coerce').fillna(0).clip(upper=1e6)
            
            report = [f"--- Simulation Accuracy ---", f"Mined Transactions: {len(mined)}"]
            
            if 'actual_profit_usd' in mined.columns:
                mined['actual_profit_usd'] = pd.to_numeric(mined['actual_profit_usd'], errors='coerce').fillna(0).clip(upper=1e6)
                mined['profit_delta'] = mined['actual_profit_usd'] - mined['estimated_profit_usd']
                mae = mined['profit_delta'].abs().mean()
                report.append(f"Mean Absolute Profit Error: ${mae:.4f}")
                report.append(f"Avg Simulated Profit: ${mined['estimated_profit_usd'].mean():.4f}")
                report.append(f"Avg Actual Profit:    ${mined['actual_profit_usd'].mean():.4f}")
            else:
                report.append("Note: 'actual_profit_usd' field missing. Accuracy based on gas consumption.")
                if 'gas_used' in mined.columns:
                    report.append(f"Avg Gas Used: {pd.to_numeric(mined['gas_used'], errors='coerce').mean():.0f}")
                if 'total_fee_eth' in mined.columns:
                    report.append(f"Avg Total Fee: {pd.to_numeric(mined['total_fee_eth'], errors='coerce').mean():.6f} ETH")

            return "\n".join(report) + "\n"
        except Exception as e:
            log.error(f"Accuracy analysis error: {e}")
            return f"Error analyzing accuracy: {e}"

    def _analyze_competition(self) -> str:
        """Analyzes shadow trades and competitors."""
        shadow_file = self.log_dir / "shadow_trades.jsonl"
        comp_file = self.log_dir / "competitors.jsonl"
        
        report = ["--- Competition Forensics ---"]
        
        if shadow_file.exists():
            try:
                df = pd.read_json(shadow_file, lines=True)
                if not df.empty:
                    report.append(f"Total Shadow Trades Seen: {len(df)}")
                    if 'router' in df.columns:
                        top_routers = df['router'].value_counts().head(5)
                        report.append(f"Top Routers Targetted:\n{top_routers.to_string()}")
            except Exception as e:
                report.append(f"Error reading shadow trades: {e}")

        if comp_file.exists():
            try:
                df = pd.read_json(comp_file, lines=True)
                if not df.empty:
                    report.append(f"Detailed Competitor Matches: {len(df)}")
                    if 'winner_eoa' in df.columns:
                        top_bots = df['winner_eoa'].value_counts().head(5)
                        report.append(f"Top Competitor Wallets:\n{top_bots.to_string()}")
                    if 'total_fee_eth' in df.columns:
                        avg_fee = df['total_fee_eth'].mean()
                        report.append(f"Avg Competitor Fee: {avg_fee:.6f} ETH")
            except Exception as e:
                report.append(f"Error reading competitors: {e}")

        if len(report) == 1:
            return "No competition logs found."
            
        return "\n".join(report) + "\n"

    def _generate_visualizations(self) -> str:
        """Generates PnL curves and distribution plots."""
        opp_file = self.log_dir / "lifecycle.jsonl"
        if not opp_file.exists():
            return "No data for visualizations."

        try:
            df = pd.read_json(opp_file, lines=True)
            if df.empty or 'open_time' not in df.columns:
                return "Data too sparse for visualizations."

            # 1. Potential PnL Curve
            df_plot = df.sort_values('open_time')
            df_plot['cum_profit'] = df_plot['peak_profit_usd'].cumsum()
            df_plot['time_dt'] = pd.to_datetime(df_plot['open_time'], unit='s')

            plt.figure(figsize=(12, 6))
            plt.plot(df_plot['time_dt'], df_plot['cum_profit'], label='Cumulative Potential Profit', color='#00ff41', linewidth=2)
            plt.fill_between(df_plot['time_dt'], df_plot['cum_profit'], color='#00ff41', alpha=0.1)
            plt.title('Potential Market Profit Over Time (PnL Curve)', fontsize=14, color='white')
            plt.xlabel('Time', fontsize=12, color='white')
            plt.ylabel('USD Profit (Theoretical)', fontsize=12, color='white')
            plt.grid(True, linestyle='--', alpha=0.3)
            plt.gca().set_facecolor('#1e1e1e')
            plt.gcf().set_facecolor('#1e1e1e')
            plt.tick_params(colors='white')
            
            pnl_path = self.viz_dir / f"pnl_curve_{datetime.now().strftime('%Y%m%d')}.png"
            plt.savefig(pnl_path, dpi=300, bbox_inches='tight', facecolor='#1e1e1e')
            plt.close()

            # 2. Opportunity Type Distribution
            plt.figure(figsize=(10, 6))
            df['opp_type'].value_counts().plot(kind='bar', color='#3498db')
            plt.title('Opportunity Distribution by Type', fontsize=14, color='white')
            plt.ylabel('Frequency', fontsize=12, color='white')
            plt.gca().set_facecolor('#1e1e1e')
            plt.gcf().set_facecolor('#1e1e1e')
            plt.tick_params(colors='white')
            
            dist_path = self.viz_dir / f"opp_distribution_{datetime.now().strftime('%Y%m%d')}.png"
            plt.savefig(dist_path, dpi=300, bbox_inches='tight', facecolor='#1e1e1e')
            plt.close()

            return f"Generated 2 plots in {self.viz_dir}:\n- {pnl_path.name}\n- {dist_path.name}"
        except Exception as e:
            log.error(f"Visualization error: {e}")
            return f"Failed to generate visualizations: {e}"

if __name__ == "__main__":
    # Quick standalone test
    reporter = ResearchReporter()
    print(reporter.generate_full_analysis())
    reporter.export_all_to_csv()

