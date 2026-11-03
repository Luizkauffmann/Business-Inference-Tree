# Visual ML import – from a Lab model to an editable strategy tree

*Import → Visual ML decision tree* takes a decision tree trained in Dataiku's Lab (Visual ML) and rebuilds it as an ordinary strategy tree. Every threshold becomes visible, and every leaf can be renamed, edited, labelled, exported and sent to Strategy Studio.

**In one sentence:** machine learning finds the structure; you turn it into policy.

> Needs a Dataiku project: the browser demo has no Lab. Every number below comes from `tests/uci_vml_example.py`, which reproduces the Lab's preprocessing on the UCI sample.

## What the import does

| Model part | Becomes |
| --- | --- |
| Numeric split on a rescaled feature, e.g. `months_late_6m <= 0.416` | A split on the **real scale**: `months_late_6m < 1.5`. Raw, avg/std (before or after imputation) and min/max un-rescaling are tried. The one that reproduces the model's own node sample counts wins, and the threshold is snapped to the midpoint between adjacent data values. |
| Dummy-encoded category, e.g. `dummy:education:university` | A category split: `education = university` vs the rest |
| "Is missing" dummy | A rule split `notnull(col)` |
| Missing numeric values | Routed the way the Lab's mean imputation sends them |
| Leaf class probabilities | **APPROVE** if P(approve class) ≥ the threshold, otherwise **DECLINE** with reason `MODEL_DECLINE`. An output `model_p_<class>` keeps the probability. |

The import report shows each threshold conversion and the **max share gap**: each leaf's share of the loaded rows vs its share of the model's training rows. A gap near 0 means a faithful rebuild.

## Example 1 – a risk tree on the UCI sample

**Data:** `credit_card_default.csv` from `docs/demo/data/` (30,000 customers, `default_next_month` = 22.3 % events, a 50/50 `sample` column).

**1. In Dataiku**
1. Upload the CSV as a dataset, e.g. `credit_card_default`.
2. **Lab → AutoML prediction**, target `default_next_month`.
3. **Train / test:** to mirror the example, use *explicit extracts*:
   - train = `sample == 'development'`;
   - test = `sample == 'validation'`.
4. **Features:**
   - numeric `pay_status_m1`, `pay_status_m2`, `max_delinquency_6m`, `months_late_6m`, `utilization_m1`, `avg_utilization_6m`, `payment_ratio_m1`, `credit_limit`, all with the default avg/std rescaling and mean imputation;
   - categorical `education` (dummy encoding).
   - Reject `customer_id`, `sample` and `behaviour_score`.
5. **Algorithm:** Decision tree, max depth 4, min samples per leaf 100. Train.

**2. In the Strategy Tree Builder**
1. Load `credit_card_default` with target `default_next_month` and score `behaviour_score`.
2. *Import → Visual ML decision tree* → pick the tree.
   - **Approve class** `0` (no default). It is also chosen automatically.
   - **P(approve) ≥ 0.8.** With a 22 % event rate, 0.5 would approve almost everyone.

**3. What you get** (development half loaded, results on the validation half)

| Check | Result |
| --- | --- |
| Leaves | 14 |
| Max share gap | 0.0 |
| Root split | `pay_status_m1 < 1.5` (at most 1 month late last month), then `months_late_6m` on both sides |
| Threshold notes | `months_late_6m: 0.4164 -> 1.5 (avg/std un-rescaled)`, `credit_limit: 0.9813 -> 295000 (avg/std un-rescaled)` |
| Agreement with the model's own predictions | **100 %** of 15,010 validation rows |
| Approval | 68.4 % |
| Event rate | approved 12.1 % · declined 44.3 % · overall 22.3 % |
| *Send to Studio* | Every split converts; Strategy Studio routes 100 % of rows identically. The only items listed are the `model_p_0` outputs, which Studio cannot carry. |

**4. Make it policy**
- **Rename** leaves, e.g. *Current, low utilisation*.
- **Merge** leaves the model split for little gain: ✎ *Edit this split* → select bins → *Merge*.
- **Labels:** add a `Rating` or `Segment` to branches (see [labels](labels.md)).
- **Score cut-offs:** switch a leaf from the model probability to a **score cut-off** on `behaviour_score` where a score rule is preferred.
- **Calibration:** set a holdout, **Freeze leaf rates** and check **Calibration…**. Then **Save**, **Download** SQL / Python, or **Send to Studio**.

## Example 2 – a surrogate of an existing decision

With a past decision column (e.g. `existing_decision` = Approve / Decline), the same steps rebuild **the current policy as a readable tree**: train the Lab tree on the decision instead of the outcome.

1. **Lab:** target = the decision column.
2. **Features:** only attributes **known at decision time**. Exclude every performance field, e.g. *ever 60 days past due in the next 12 months*. Dataiku's leakage diagnostic flags such fields; a surrogate dominated by one of them encodes outcomes, not policy.
3. **Import:** approve class = `Approve`.
   - **Threshold:** to mirror the Lab's predictions, use `1 − (the model's active threshold on Decline)`. For example, 0.775 on Decline gives P(approve) ≥ 0.225.
4. **Fidelity check:** *Node metrics…* → custom metric **rate** of `existing_decision == 'Approve'`. Each card then shows the bank's approval next to the tree's. Leaves where they disagree are where hidden rules or manual overrides live.
5. **Outcomes:** add the outcome as the target and read the event rates per leaf.
   - Declined leaves have few observed outcomes (never booked): treat their rates as unknown.
6. **Hand-off:** *Send to Studio* as the **champion**, then compare challengers against it with the swap set.

A surrogate is an approximation of the policy (check its AUC and the per-leaf fidelity), and it inherits whatever the model learned. Review it like any strategy, including a fair-lending review, before using it.

## Limitations

- **Random forest:** only one tree is imported (the first by default), not the forest's averaged prediction.
- **Preprocessing:** numeric (raw or rescaled), dummy-encoded categories and missing values are supported. Impact coding, hashing, derived features and text are reported as not supported.
- **Binary trees:** Lab trees split two ways at each node. Merge or re-split with the bin editor, or use **Optimal split** for multi-way splits.
- **Validation:** tests compare against scikit-learn trees built with the Lab's preprocessing. On a real Lab model, read the import report: the max share gap tells you whether the rebuild is faithful.
