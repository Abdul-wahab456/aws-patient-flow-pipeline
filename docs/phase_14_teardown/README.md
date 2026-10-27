# Phase 14 — Final README and teardown

## The README
`README.md` in the project root is the portfolio page: the problem, the architecture (Mermaid diagram), the data model, the KPIs with charts, how the quality is checked, the CI/CD design, how to run it, cost, lessons learned and future improvements. Charts come from `notebooks/kpi_analysis.ipynb` and are stored in `docs/images/`.

## Teardown: delete everything the project created
`infra/teardown.py` removes the project from the AWS account so nothing keeps costing money. It never runs by itself.

```
python infra/teardown.py --plan                # prints what would be deleted; deletes nothing
python infra/teardown.py --delete-everything   # asks you to type the lake bucket name, then deletes
```
Order of a full teardown: stack `pf-pipeline` → stack `pf-foundation` → the results bucket (emptied) → the lake bucket (emptied) → stack `pf-cicd`. The buckets come after their stack because the stack keeps them on purpose (`DeletionPolicy: Retain`). Every delete skips what is already gone, so a teardown that stopped half way can be started again.

Not deleted (not created by this project's stacks): the GitHub login provider of the account (it can be shared by other repositories), the budget alert, and the GitHub repository variables. The IAM user you work as is yours.

## Checked
* `tests/test_teardown.py`: the deletion order (trigger and workflow first, roles after the things that use them, data last), that every kind in the plan has a deleter, that deleting something absent is not an error, that a bucket is emptied before it is deleted — all with fake AWS clients.
* The same deletion code removed the trial stacks and buckets of Phase 11 in us-west-2 (see `docs/phase_11_cloudformation`) and retired the hand-built resources in us-east-1.
* **The final teardown of the real project has not been run** and is left for you to decide.
