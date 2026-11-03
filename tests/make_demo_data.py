"""Synthetic credit-application data for the tests and the docs (no real data).

    python tests/make_demo_data.py [out.csv] [n_rows]

One latent risk factor drives bureau / application attributes and a 12-month bad flag.
``bad_latent_synthetic`` is the outcome of every applicant; ``bad_12m_flag_demo`` keeps it
only for approved (booked) applications, as in real credit data.
"""
import sys

import numpy as np
import pandas as pd


def make(n=12000, seed=7):
    g = np.random.default_rng(seed)
    r = g.normal(size=n)                                          # latent risk, higher = riskier
    states = np.array(["CA", "TX", "FL", "NY", "IL", "PA", "OH", "GA", "NC", "MI", "WA", "AZ", "TN", "SC", "CO", "VA"])
    st = g.choice(states, n, p=np.r_[[0.14, 0.11, 0.09, 0.08, 0.06, 0.06, 0.05, 0.05, 0.05, 0.05], np.full(6, 0.26 / 6)])
    st_eff = pd.Series(g.normal(0, 0.25, len(states)), index=states)[st].values
    thin = (g.random(n) < 0.08).astype(int)
    vantage = np.clip(np.round(690 - 55 * r + g.normal(0, 25, n)), 300, 850)
    internal = np.round(600 - 40 * r + g.normal(0, 30, n))
    dti = np.clip(30 + 8 * r + g.normal(0, 8, n), 1, 80).round(2)
    util = np.clip(45 + 15 * r + g.normal(0, 20, n), 0, 100).round(1)
    income = np.round(np.exp(g.normal(8.6, 0.45, n)), 0)
    amount = np.round(np.exp(g.normal(9.8, 0.6, n)) / 100) * 100
    term = g.choice([36, 48, 60, 72], n)
    payment = amount / term * 1.12
    df = pd.DataFrame({
        "application_id": [f"APP{100000 + i}" for i in range(n)],
        "decision_month": pd.to_datetime("2025-01-01") + pd.to_timedelta(g.integers(0, 12, n) * 30, unit="D"),
        "state": st,
        "requested_amount": amount, "requested_term_months": term, "stated_monthly_income": income,
        "estimated_payment_to_income_pct": (100 * payment / income).round(2),
        "ltv_pct": np.clip(95 + 10 * r + g.normal(0, 15, n), 40, 160).round(1),
        "vantage_score": vantage, "debt_to_income_pct": dti, "revolving_utilization_pct": util,
        "inquiries_6m_count": g.poisson(np.exp(0.3 + 0.4 * r)),
        "delinquencies_24m_count": g.poisson(np.exp(-2.0 + 0.9 * r)),
        "collections_count": g.poisson(np.exp(-2.5 + 0.8 * r)),
        "thin_file_flag": thin,
        "avg_credit_age_years": np.clip(8 - 2 * r + g.normal(0, 2.5, n), 0.2, 40).round(1),
        "open_tradelines_count": g.poisson(np.exp(1.8 - 0.15 * r)),
        "member_tenure_years": np.round(g.gamma(2, 3, n), 1),
        "internal_behavior_score": internal,
    })
    df["channel"] = g.choice(["branch", "online", "mobile", "dealer"], n, p=[0.2, 0.4, 0.3, 0.1])
    df["collateral_value"] = np.where(g.random(n) < 0.6, np.round(amount / (df["ltv_pct"] / 100), -2), np.nan)
    df["fico_score_proxy"] = np.clip(np.round(vantage + g.normal(0, 18, n)), 300, 850)
    df["bankruptcy_flag"] = (g.random(n) < 1 / (1 + np.exp(-(-4.2 + 0.9 * r)))).astype(int)
    for c, share in (("vantage_score", 0.02), ("debt_to_income_pct", 0.03), ("revolving_utilization_pct", 0.04)):
        df.loc[g.random(n) < share, c] = np.nan
    df["policy_score_existing"] = np.clip(np.round(0.55 * vantage + 0.45 * (internal + 200) - 20 * thin + g.normal(0, 15, n)), 300, 900)
    pd_true = 1 / (1 + np.exp(-(-3.1 + 1.15 * r + 0.35 * thin + st_eff)))
    df["bad_latent_synthetic"] = (g.random(n) < pd_true).astype(int)
    cut = np.quantile(df["policy_score_existing"], 0.3)
    approve = (df["policy_score_existing"] >= cut) & (df["delinquencies_24m_count"] < 3)
    df["existing_decision"] = np.where(approve, "Approve", "Decline")
    df["bad_12m_flag_demo"] = np.where(approve, df["bad_latent_synthetic"], np.nan)
    df["bad_12m_flag"] = df["bad_12m_flag_demo"]
    # surrogate-model style scores of the existing decision (as written by a Visual ML scoring)
    z = (df["policy_score_existing"] - cut) / 25 - 0.8 * (df["delinquencies_24m_count"] >= 3) + g.normal(0, 0.6, n)
    df["proba_Approve"] = (1 / (1 + np.exp(-z))).round(4)
    df["proba_Decline"] = (1 - df["proba_Approve"]).round(4)
    df["prediction"] = np.where(df["proba_Approve"] >= 0.5, "Approve", "Decline")
    return df


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "demo_applications.csv"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 12000
    d = make(n)
    d.to_csv(out, index=False)
    print(f"{out}: {len(d)} rows, event rate {np.nanmean(d['bad_12m_flag_demo']):.3%} (booked), "
          f"{d['bad_latent_synthetic'].mean():.3%} (all applicants)")
