# Logic tab and formula language

Select a node, open **Logic**, edit, then click **Apply logic**. Every edit is re-evaluated on the whole population straight away.

### Order of evaluation for each row

1. **Calculations** of the root, then of each node on the row's path, top-down. A calculation exists only for rows that reach its node, and every node *below* can use it like a column. Each one is also written to the output as `calc_<name>`.
2. **Splits** route the row: numeric cuts, category groups or **rules** (first true rule wins, last branch = *Otherwise*). A split can use a calculation as its variable.
3. At the **leaf**, in this order:
   - **score formula** (blank = the *Score column*), which gives `tree_score`;
   - **decision**: *score cut-off* gives APPROVE if score ≥ *Approve at*, REFER if ≥ *Refer at*, otherwise DECLINE (a missing score gives REFER); or a fixed APPROVE / REFER / DECLINE;
   - **reason codes** and **pricing tier** (fixed text);
   - **credit-limit multiplier** (number or formula, blank = 1.0);
   - **outputs**: extra named formulas, written as `out_<name>`;
   - **predicted event rate** (`tree_proba_1` / `tree_event_rate`, see [Event-rate prediction](event-rate-prediction.md)).

Formulas use a safe language:
- column and calculation names, numbers, `+ - * / **`, comparisons;
- `and` / `or` / `not`, `in [...]`, `a if condition else b`;
- the functions `min`, `max`, `abs`, `round`, `floor`, `ceil`, `log`, `exp`, `sqrt`, `clip`, `where`, `coalesce`, `isnull`, `notnull`, `between` and `col('name with spaces')`.

A comparison with a missing value is false. The same formula runs in the app, in the SQL export and in the Python export.

### Worked example

It runs on the synthetic data from `tests/make_demo_data.py`; `tests/wiki_examples.py` builds this exact tree and checks that the SQL and Python exports match the app on every row.

**Root node → Calculations**

| Name | Formula | Meaning |
| --- | --- | --- |
| `adj_score` | `policy_score_existing - 15 * inquiries_6m_count` | Score penalised 15 points per recent inquiry |
| `dti_stress` | `debt_to_income_pct * 1.1 + 5` | Stressed debt-to-income (+10 % and +5 points) |
| `loan_to_income` | `requested_amount / max(stated_monthly_income * 12, 1)` | Loan amount vs annual income (no division by 0) |

**Root node → Split → + Manual / rule split** (one rule per line):

```
delinquencies_24m_count > 0 or collections_count > 0
thin_file_flag == 1 and member_tenure_years < 2
```

This gives three branches: *Derogatory*, *Thin file, new member* and *Otherwise* (clean file).

**Leaves of the rule split**

| Leaf | Decision | Reason codes |
| --- | --- | --- |
| Derogatory | DECLINE | `DELINQ_OR_COLLECTION` |
| Thin file, new member | REFER | `THIN_FILE` |

**Clean-file branch → numeric split on the calculation** `dti_stress`, cuts `35, 50` (missing → last branch). Its three leaves:

| Leaf | Score formula | Approve at / Refer at | Tier | Credit-limit multiplier | Outputs |
| --- | --- | --- | --- | --- | --- |
| Low DTI (< 35) | `adj_score` | 760 / 740 | A | `clip(1.5 - dti_stress / 100, 0.5, 1.2)` | `apr` = `0.199 if vantage_score < 640 else 0.149`; `max_amount` = `round(stated_monthly_income * 3, 0)` |
| Mid DTI [35, 50) | `where(revolving_utilization_pct > 80, adj_score - 20, adj_score)` | 780 / 760 | B, reason `HIGH_UTIL` | `0.8` | – |
| High DTI ≥ 50 | `adj_score - 30` | 800 / – | C, reason `HIGH_DTI` | `0.6 if loan_to_income > 0.3 else 0.8` | – |

**Result columns for one approved applicant:**
- `tree_leaf` = the *Low DTI* leaf, `tree_score` = 801, `tree_decision` = APPROVE;
- `limit_multiplier` = 1.155, `out_apr` = 0.149, `out_max_amount` = 15,581;
- `calc_adj_score` = 801, `calc_dti_stress` = 34.48;
- plus `tree_proba_1` / `tree_prediction` / `tree_event_rate`.

The SQL and Python exports of this tree reproduce 100 % of its decisions.

**Tips**
- A formula can only use calculations defined on the same node or above it. **Validate** reports unknown names.
- Strategy Studio cannot represent calculations, rule splits or formulas. *Send to Studio* lists them and asks you to confirm a simplified version.

## Formula language reference

| Element | Syntax |
| --- | --- |
| Names | Column names and calculation names; `col('name with spaces')` for any other column name |
| Literals | Numbers, `'text'` / `"text"`, `True`, `False`, `None` |
| Arithmetic | `+  -  *  /  //  %  **` (division by zero gives missing) |
| Comparisons | `<  <=  >  >=  ==  !=`, chained comparisons (`300 <= x < 600`) |
| Logic | `and`, `or`, `not` |
| Membership | `x in ['CA', 'NY']`, `x not in [...]` |
| Conditional | `a if condition else b` |
| Functions | `min`, `max` (element-wise, several arguments), `abs`, `round(x, digits)`, `floor`, `ceil`, `log`, `exp`, `sqrt`, `clip(x, low, high)`, `where(cond, a, b)`, `coalesce(a, b, …)`, `isnull`, `notnull`, `between(x, low, high)` (inclusive) |

**Missing values**
- Any comparison with a missing value is false, so a rule on a missing attribute falls through to the next rule.
- `isnull` / `coalesce` handle missing values explicitly.
- Arithmetic with a missing value gives missing; a missing score at a cut-off leaf gives REFER.

**Safety.** Formulas are parsed with Python's `ast` and only the elements above are allowed: no attribute access, subscripts, lambdas, comprehensions or imports. Unknown names are reported by **Validate**.

**One formula, three runtimes.** Every formula is evaluated:
- vectorised over pandas in the app;
- translated to **SQL** (`CASE WHEN`, `COALESCE`, `GREATEST` / `LEAST`, `NULLIF` for division) for the SQL export;
- translated to row-level **Python** for the `decide(row)` export.

The test suite checks that the three agree on every row.

