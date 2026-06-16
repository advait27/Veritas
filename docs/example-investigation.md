# A worked investigation

This walks the Veritas loop end to end on a tiny dataset, the way Claude would drive it
through the MCP tools. The mechanics shown here are exercised in
[`tests/test_example_investigation.py`](../tests/test_example_investigation.py), so the
behavior — including the verification outcomes — is real, not illustrative.

The data is six rows of monthly revenue by region:

| region | month | revenue |
| - | - | - |
| east | jan | 120 |
| west | jan | 100 |
| east | feb | 130 |
| west | feb | 90 |
| east | mar | 125 |
| west | mar | 40 |

**The goal:** "Which region is weakest, and by how much?"

## 1. Orient

```text
ingest_dataset(path="orders.csv", name="orders")
  → { dataset_id: "ds_…", row_count: 6, columns: [region, month, revenue] }

profile_dataset(dataset_id="ds_…")
  → markdown: 3 columns, no nulls, revenue ranges 40–130, two regions, three months
```

Read the profile before asking anything. Here it confirms the shape and that `revenue` is
numeric — so a sum is meaningful.

## 2–3. Branch, then falsify with a query

The hypothesis tree for "which region is weakest" is small and MECE: *east is weakest* vs.
*west is weakest*. One query falsifies one of them:

```text
run_sql(sql="SELECT region, sum(revenue) AS total FROM ds_… GROUP BY 1")
  → { artifact_id: "art_…", status: "ok",
      preview: | region | total |
               | east   | 375   |
               | west   | 230   | }
```

West is weaker. The `artifact_id` is the receipt; every number below must trace to it.

## 4. Record the finding as a claim, and verify it

```text
record_finding(
  headline="West's total revenue is 230",
  claims=[{ description: "west total revenue",
            artifact_id: "art_…", column: "total",
            where: { region: "west" }, value: 230.0 }])
  → { finding_id: "fnd_…", status: "unverified" }

verify_finding(finding_id="fnd_…")
  → { verified: true, status: "verified", unbacked_numbers: [] }
```

The claim pins `230` to the `total` cell **where `region = "west"`** — by key, never by row
position. `verify_finding` re-reads that cell from the artifact's Parquet, sees `230`, and
the only number in the prose (`230`) is backed. Verified — this finding may enter a report.

## The receipts rule has teeth

Now write a finding with a number that traces to nothing:

```text
record_finding(headline="West collapsed in 2024", claims=[])
verify_finding(finding_id="fnd_…")
  → { verified: false, status: "refuted", unbacked_numbers: ["2024"] }
```

`2024` is a real number in the prose with no claim behind it, so the finding is **refused** —
fail-closed. Either back it with a claim or remove the digit. This is exactly the
silently-untraceable figure Veritas exists to prevent.

## What the loop guarantees

Nothing reached a report on trust. The one number that survived (`230`) came from an
execution Veritas recorded and re-checked in deterministic Python; the one that could not be
traced (`2024`) was rejected. That is the whole promise: **receipts, or it didn't happen.**
