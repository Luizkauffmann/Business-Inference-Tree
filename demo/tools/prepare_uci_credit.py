"""Build demo/data/credit_card_default.csv from the UCI "Default of Credit Card Clients" dataset.

Source: Yeh, I-C. & Lien, C-H. (2009). The comparisons of data mining techniques for the predictive
accuracy of probability of default of credit card clients. Expert Systems with Applications 36(2).
UCI Machine Learning Repository, https://doi.org/10.24432/C55S3H – licensed CC BY 4.0.

    python demo/tools/prepare_uci_credit.py "default of credit card clients.xls" demo/data/credit_card_default.csv

Changes from the original (allowed by CC BY 4.0, listed here as the licence requires):
- columns renamed to readable names; months relabelled m1 (most recent, Sept 2005) ... m6 (April 2005);
- SEX, MARRIAGE and AGE removed, so the demo does not decide on protected characteristics;
- EDUCATION decoded to labels;
- derived features added (utilisation, delinquency counts, payment ratio);
- `sample` (development / validation, 50/50, seed 42) and `behaviour_score`, a simple logistic
  scorecard fitted on the development sample only (600 points at 50:1 good:bad odds, 20 points to
  double the odds) – a demo score, not a validated model.
"""
import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

src, out = sys.argv[1], sys.argv[2]
raw = pd.read_excel(src, header=1)
d = pd.DataFrame({"customer_id": raw["ID"], "credit_limit": raw["LIMIT_BAL"]})
d["education"] = raw["EDUCATION"].map({1: "graduate_school", 2: "university", 3: "high_school", 4: "other"}).fillna("unknown")
for k, c in enumerate(["PAY_0", "PAY_2", "PAY_3", "PAY_4", "PAY_5", "PAY_6"], start=1):
    d[f"pay_status_m{k}"] = raw[c]
for k in range(1, 7):
    d[f"bill_amt_m{k}"] = raw[f"BILL_AMT{k}"]
for k in range(1, 7):
    d[f"paid_amt_m{k}"] = raw[f"PAY_AMT{k}"]
status = d[[f"pay_status_m{k}" for k in range(1, 7)]]
d["months_late_6m"] = (status > 0).sum(axis=1)
d["max_delinquency_6m"] = status.clip(lower=0).max(axis=1)
d["utilization_m1"] = (d["bill_amt_m1"] / d["credit_limit"]).round(4)
d["avg_utilization_6m"] = (d[[f"bill_amt_m{k}" for k in range(1, 7)]].mean(axis=1) / d["credit_limit"]).round(4)
due = d["bill_amt_m2"].where(d["bill_amt_m2"] > 0)
d["payment_ratio_m1"] = (d["paid_amt_m1"] / due).clip(upper=5).round(4)   # missing when nothing was due
d["default_next_month"] = raw["default payment next month"].astype(int)

rng = np.random.RandomState(42)
d["sample"] = np.where(rng.rand(len(d)) < 0.5, "development", "validation")
feats = ["months_late_6m", "max_delinquency_6m", "pay_status_m1", "utilization_m1", "avg_utilization_6m"]
X = d[feats].copy()
X["log_limit"] = np.log1p(d["credit_limit"])
X["paid_ratio"] = d["payment_ratio_m1"].fillna(1.0)
dev = d["sample"] == "development"
m = LogisticRegression(max_iter=2000).fit((X[dev] - X[dev].mean()) / X[dev].std(), d.loc[dev, "default_next_month"])
p = m.predict_proba((X - X[dev].mean()) / X[dev].std())[:, 1]
d["behaviour_score"] = np.round(600 + 20 / np.log(2) * (np.log((1 - p) / p) - np.log(50))).astype(int)
d.to_csv(out, index=False)
print(f"{out}: {len(d)} rows, {d.shape[1]} columns, default rate {d['default_next_month'].mean():.2%}")
