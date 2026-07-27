# Documentation drift queue

Generated 2026-07-27 by scripts/check_docs.py against the tree after Phase 2.
This is the Phase 4 work queue. 62 failures: 54 link, 5 path, 3 counted.

```
counted  website/docs/explanation/architecture.md:355  claims 8 bundled config profiles, tree has 4
counted  website/docs/explanation/data-flow.md:493  claims 8 bundled config profiles, tree has 4
counted  website/docs/reference/api.md:1024  claims 8 bundled config profiles, tree has 4
path     website/docs/explanation/architecture.md:157  references missing services/gpas/lb/nginx.conf
path     website/docs/explanation/architecture.md:157  references missing services/nlp/nginx.conf
path     website/docs/explanation/data-flow.md:326  references missing services/gpas/lb/nginx.conf
path     website/docs/explanation/data-flow.md:326  references missing services/nlp/nginx.conf
path     website/docs/reference/components/infrastructure.md:119  references missing services/anonymizer/sql/init.sql
link     docs/trust-gate/complete-guide.md:182  unresolved ../services/anonymizer/src/pipeline/intake_gate.py
link     docs/trust-gate/complete-guide.md:187  unresolved ../services/trust-gate/src/api/service.py
link     docs/trust-gate/complete-guide.md:199  unresolved ../services/trust-gate/src/engine.py
link     docs/trust-gate/complete-guide.md:201  unresolved ../services/trust-gate/src/verdict/registry.py
link     docs/trust-gate/complete-guide.md:209  unresolved ../services/trust-gate/src/constants.py
link     docs/trust-gate/complete-guide.md:255  unresolved ../services/trust-gate/src/baseline.py
link     docs/trust-gate/complete-guide.md:264  unresolved ../services/trust-gate/src/passport.py
link     docs/trust-gate/complete-guide.md:284  unresolved ../services/trust-gate/src/constants.py
link     docs/trust-gate/complete-guide.md:299  unresolved ../services/trust-gate/src/verdict/scoring.py
link     docs/trust-gate/complete-guide.md:315  unresolved ../services/trust-gate/src/dimensions.py
link     docs/trust-gate/complete-guide.md:331  unresolved ../services/trust-gate/src/verdict/decision.py
link     docs/trust-gate/complete-guide.md:359  unresolved ../services/trust-gate/src/verdict/fitness.py
link     docs/trust-gate/complete-guide.md:379  unresolved ../services/trust-gate/src/verdict/coverage.py
link     docs/trust-gate/complete-guide.md:399  unresolved ../services/trust-gate/src/checks/conformance/__init__.py
link     docs/trust-gate/complete-guide.md:424  unresolved ../services/trust-gate/src/checks/completeness.py
link     docs/trust-gate/complete-guide.md:441  unresolved ../services/trust-gate/src/checks/plausibility.py
link     docs/trust-gate/complete-guide.md:466  unresolved ../services/trust-gate/src/checks/governance.py
link     docs/trust-gate/complete-guide.md:493  unresolved ../services/trust-gate/src/validator_client.py
link     docs/trust-gate/complete-guide.md:513  unresolved ../services/trust-gate/src/terminology_client.py
link     docs/trust-gate/data-quality-process.md:24  unresolved ../services/anonymizer/src/pipeline/intake_gate.py
link     docs/trust-gate/data-quality-process.md:35  unresolved ../services/trust-gate/src/api/service.py
link     docs/trust-gate/data-quality-process.md:59  unresolved ../services/trust-gate/src/engine.py
link     docs/trust-gate/data-quality-process.md:64  unresolved ../services/trust-gate/src/verdict/runner.py
link     docs/trust-gate/data-quality-process.md:65  unresolved ../services/trust-gate/src/verdict/registry.py
link     docs/trust-gate/data-quality-process.md:86  unresolved ../services/trust-gate/src/passport.py
link     docs/trust-gate/data-quality-process.md:121  unresolved ../services/trust-gate/src/constants.py
link     docs/trust-gate/data-quality-process.md:127  unresolved ../services/trust-gate/src/verdict/scoring.py
link     docs/trust-gate/data-quality-process.md:143  unresolved ../services/trust-gate/src/verdict/coverage.py
link     docs/trust-gate/data-quality-process.md:153  unresolved ../services/trust-gate/src/verdict/decision.py
link     docs/trust-gate/data-quality-process.md:163  unresolved ../services/trust-gate/src/verdict/fitness.py
link     docs/trust-gate/data-quality-process.md:202  unresolved ../services/trust-gate/src/passport.py
link     docs/trust-gate/data-quality-process.md:210  unresolved ../services/trust-gate/src/api/service.py
link     docs/trust-gate/data-quality-process.md:245  unresolved ../services/trust-gate/src/baseline.py
link     docs/trust-gate/how-it-works.md:146  unresolved ../services/trust-gate/src/checks/completeness.py
link     docs/trust-gate/how-it-works.md:153  unresolved ../services/trust-gate/src/constants.py
link     docs/trust-gate/how-it-works.md:193  unresolved ../services/trust-gate/src/phases.py
link     docs/trust-gate/how-it-works.md:209  unresolved ../services/trust-gate/src/dimensions.py
link     docs/trust-gate/how-it-works.md:220  unresolved ../services/trust-gate/src/verdict/scoring.py
link     docs/trust-gate/how-it-works.md:238  unresolved ../services/trust-gate/src/dimensions.py
link     docs/trust-gate/how-it-works.md:257  unresolved ../services/trust-gate/src/verdict/decision.py
link     docs/trust-gate/how-it-works.md:294  unresolved ../services/trust-gate/src/verdict/fitness.py
link     docs/trust-gate/how-it-works.md:316  unresolved ../services/trust-gate/src/verdict/coverage.py
link     docs/trust-gate/how-it-works.md:324  unresolved ../services/trust-gate/src/verdict/scoring.py
link     docs/trust-gate/how-it-works.md:366  unresolved ../services/trust-gate/src/checks/conformance/__init__.py
link     docs/trust-gate/how-it-works.md:394  unresolved ../services/trust-gate/src/validator_client.py
link     docs/trust-gate/how-it-works.md:405  unresolved ../services/trust-gate/src/checks/completeness.py
link     docs/trust-gate/how-it-works.md:428  unresolved ../services/trust-gate/src/checks/plausibility.py
link     docs/trust-gate/how-it-works.md:457  unresolved ../services/trust-gate/src/constants.py
link     docs/trust-gate/how-it-works.md:466  unresolved ../services/trust-gate/src/checks/governance.py
link     docs/trust-gate/how-it-works.md:481  unresolved ../services/trust-gate/src/verdict/decision.py
link     docs/trust-gate/how-it-works.md:505  unresolved ../services/trust-gate/src/validator_client.py
link     docs/trust-gate/how-it-works.md:528  unresolved ../services/trust-gate/src/terminology_client.py
link     docs/trust-gate/how-it-works.md:542  unresolved ../services/trust-gate/src/verdict/registry.py
link     docs/trust-gate/how-it-works.md:546  unresolved ../services/trust-gate/src/verdict/fitness.py
docs: 48 checked, 62 failure(s)
```
