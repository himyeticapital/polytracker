Product Requirements Document (PRD): Polymarket Backtest Engine & Data Collector v2
1. Executive Summary
Objective: Build a professional-grade, local Python system to collect Polymarket order-book data, store it efficiently, and use Jupyter Notebooks to visually backtest and refine trading strategies.

2. Core Modules
Module A: The Data Collector ("The Hoarder")
Source: Polymarket CLOB API & Gamma API.

Target Data: Minute-by-minute price snapshots, Volume, and "Yes/No" spreads.

Storage: DuckDB (Single file database, highly efficient) or Parquet files.

Automation: A simple script (collect.py) that can be scheduled to run every hour.

Module B: The Backtest Engine ("The Simulator")
Logic: Vectorized backtesting using Polars (faster than Pandas) or Pandas.

Input: Accepts a custom "Strategy Function" and historical data.

Calculation: Simulates a wallet balance, transaction fees, and PnL (Profit & Loss) over time.

Module C: The Visualization Layer (Jupyter Notebook)
Interface: Jupyter Lab / Notebook (.ipynb).

Charting: Use Plotly for interactive financial charts.

Features:

Candlestick Chart: Shows price history.

Trade Markers: Visual icons on the chart indicating exactly when a Buy/Sell happened.

Equity Curve: A line graph showing the growth of the $1,000 starting capital over time.

3. Technical Stack
Language: Python 3.10+

Core Libraries: requests, pandas, duckdb.

Visualization: jupyterlab, plotly, ipywidgets.

File Structure:

data/ (Where database lives)

src/ (Where the collector and engine code live)

notebooks/ (Where you run experiments and view charts)

4. User Stories
Visual Validation: As a user, I want to open analysis.ipynb, run a cell, and see an interactive chart where I can zoom in to verify if my bot bought at the "bottom" or the "top."

Strategy Injection: As a user, I want to define a strategy logic in one cell (e.g., RSI < 30 = Buy) and immediately run the backtest to see the result without restarting the program.

Data Export: As a user, I want to export the "clean" data from DuckDB to a CSV file so I can inspect it manually if needed.

5. Success Metrics
Visual Clarity: The Buy/Sell markers must be clearly visible on the Plotly chart.

Iteration Speed: I should be able to change a strategy parameter (e.g., "Moving Average 20" to "50") and see the new chart in under 3 seconds.