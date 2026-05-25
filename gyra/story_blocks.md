# GYRA Story Blocks

Each **block** bundles a list of **Tasks** (the actual hands-on steps) and a
separate, shorter list of **Acceptance Criteria** (outcomes that have to be
true when the work is done). Blocks compose: a template picks several blocks
and their tasks/ACs are merged into the new story.

Format per block:

```
## block-id — Display name
_Optional one-line description._
### Tasks
- task 1
- task 2
### ACs
- acceptance criterion 1
```

A blank line (or the next `##`) ends a block. Either section may be omitted.
For backward compatibility, bullets that aren't under any heading are treated
as ACs. Edit freely — parsed at import time.

Placeholders: anywhere you write `{Question text?}` (with the literal `?`
inside the braces) the user is prompted for that value once at apply time and
it's substituted into every occurrence. Same exact text = one question.

---

## 3d-base — 3D Model (base)
_Mesh + texture + lighting pipeline. Shared by characters and props._
### Tasks
- Paper design approved
- T-pose variant ready
- Silhouette blockmesh done
- Blockmesh tested in-game
- Base model with under {how many tris for model?} tris done
- Textured model done
- Whitebox scenario ready
- Whitebox animation tests done
- Shaded model running in-game
- LODs created and tested where needed
- Lighting tested and polished in whitebox
### ACs
- Model created with under {how many tris for model?} tris
- Textured and shaded model running in-engine
- LODs in place where needed
- Lighting tested in whitebox

## 3d-rig-anim — 3D Rig & Animation (body + face)
_Rig and animation pipeline. Add on top of `3d-base` for characters that
actually move and emote. Skip for static props._
### Tasks
- Base model body rigged
- Facial expressions sheet ready
- Facial keys in 3D model done
- Facial rigging done
- Whitebox input → animation output wiring done
- Animation unit tests in whitebox done
- Test animations created
- Blinking and base-expression wrappers done
- Eye-tracking facial keys added
- Eye / head tracking wrappers wired for the game
### ACs
- Body and facial rig functional
- Animation unit tests pass in whitebox
- Eye and head tracking work in-game

## 3d-place — 3D Place In-Game
_Final integration slice._
### Tasks
- Placed in-game (without real animation)
- Verified in-context in the target scene
### ACs
- Placed in-game in the target scene

## env-modular — Environment Modular Kit
_Reusable building-block kit for a single area._
### Tasks
- Define grid / snap unit for {kit name?}
- Block out wall, floor and ceiling primitives
- Model corner and trim pieces
- Author tileable texture set for {kit name?}
- Assemble a demo room from the kit
- Capture lighting reference scene
### ACs
- Demo room built only from {kit name?} pieces
- Kit shippable into {target area?}

## shader-base — Shader / VFX
_Author a single shader or VFX effect._
### Tasks
- Approve reference for {effect name?}
- Build shader graph / source for {effect name?}
- Document exposed parameters and ranges
- Hook {effect name?} into {trigger?}
- Profile on target hardware
### ACs
- {effect name?} fires on {trigger?} as expected
- Holds up under day, night and dark lighting

## audio-base — Audio / SFX
_Author a single SFX bundle for one in-game moment._
### Tasks
- Capture / source audio for {sound name?}
- Edit, normalise and loop where needed
- Author at least {how many variations?} variations
- Mix against reference scene
- Wire {sound name?} to its in-game trigger
### ACs
- {sound name?} plays on cue without repetition fatigue
- Sits within the mix bus budget

## ui-screen — UI Screen
_Build one full UI screen._
### Tasks
- Approve wireframe for {screen name?}
- Approve visual mock for {screen name?}
- Build {screen name?} in engine / framework
- Wire keyboard, gamepad and mouse navigation
- Add localisation hooks
- Verify responsiveness at min and max resolutions
### ACs
- {screen name?} lets the player {what should the screen let the player do?}
- Fully navigable on every supported input device

## mechanic-design — Gameplay Mechanic Design
_Design and prototype a single mechanic._
### Tasks
- Write one-pager for {mechanic name?} (problem, fantasy, inputs, outputs)
- Build paper prototype / spreadsheet for {mechanic name?}
- Build whitebox playable
- Expose tuning variables to the designer
- Script first-use / tutorial moment
- Map failure and edge cases
### ACs
- {mechanic name?} is playable end-to-end in whitebox
- Tuning lives in data, not code

## level-base — Level Design
_Design and ship one level._
### Tasks
- Write beat sheet / pacing diagram for {level name?}
- Build blockout playable end-to-end
- Set-dressing pass on {level name?}
- Lighting pass on {level name?}
- Performance pass on {level name?}
- Incorporate playtest notes
### ACs
- {level name?} is completable end-to-end without dev help
- Delivers the intended beat: {what should the level make the player feel?}

## balance-base — Balance / Tuning Pass
_Targeted balance pass on one system._
### Tasks
- Capture baseline metrics for {system to rebalance?}
- Write hypothesis for the change
- Adjust tuning values in data for {system to rebalance?}
- Run internal playtest
- Capture after-metrics
- Document change in patch notes
### ACs
- {symptom we are fixing?} no longer appears in playtest
- Before / after metrics recorded

## bug-repro — Bug Reproduction & Fix
_Reproduce, diagnose and fix one bug._
### Tasks
- Document 100% reliable repro for {what is broken?}
- Identify affected build / OS / hardware
- Find and document root cause (not just symptom)
- Implement fix
- Add regression test
- Verify on originally-failing platform
### ACs
- {what is broken?} no longer reproducible after fix
- Regression test guards against re-introduction

## hotfix-base — Hotfix (live)
_Live, urgent patch._
### Tasks
- Scope impact and affected users of {what is broken?}
- Prepare minimal-diff fix
- Cherry-pick to release branch
- Smoke-test hotfix build
- Deploy to production
- Verify post-deploy
### ACs
- {what is broken?} resolved in production
- {customer impact?} stops within one release cycle

## web-page — Web Page
_Ship one web page._
### Tasks
- Approve copy for {page path or name?}
- Approve visual mock for {page path or name?}
- Build {page path or name?} responsively (mobile → desktop)
- Accessibility pass (WCAG AA contrast, alt text, focus order)
- Set SEO meta + OG tags
- Wire analytics events
### ACs
- {page path or name?} live on production
- Passes accessibility and SEO checks

## component-base — UI Component (web/app)
_Reusable component for the design system._
### Tasks
- Document props / API for {component name?}
- Cover visual states (default, hover, focus, disabled, loading, error)
- Add Storybook / preview entry
- Write unit tests
- Use {component name?} in at least one real consumer
- Verify keyboard + screen-reader accessibility
### ACs
- {component name?} usable across the codebase from a single import
- All visual states covered and accessible

## api-base — API Endpoint
_One HTTP endpoint, ready for consumers._
### Tasks
- Document contract for {HTTP method?} {endpoint path?}
- Add input validation
- Enforce authn / authz
- Write happy-path integration test
- Write failure-path integration test
- Add structured logs + metrics for {endpoint path?}
### ACs
- {HTTP method?} {endpoint path?} returns spec-compliant responses
- Failure modes return correct status codes with useful errors

## db-mig — Database Migration
_Schema change to one table / collection._
### Tasks
- Author up + down migration for {table or collection?}
- Test migration on a copy of production
- Verify rollback
- Document backfill plan (or mark unneeded)
- Make code paths support both old and new schema during rollout
- Run in staging before production
### ACs
- {table or collection?} migrated with zero data loss
- Rollback path proven on a copy of production

## pipeline-base — Pipeline Step
_One automated step in any pipeline — build, asset cook, content pipeline, lint, test, package, deploy. Front-end or back-end. Local or remote._
### Tasks
- Define trigger for the {pipeline step name?} step in the {pipeline name?} pipeline
- Configure inputs and outputs
- Set caching / reuse strategy
- Tune to run under target time budget
- Make failure output loud and actionable
- Document the step in the {pipeline name?} pipeline docs
### ACs
- {pipeline step name?} step runs every time the {pipeline name?} pipeline does
- Step is documented and owned

## cicd-online-base — CI/CD Online Build & Checks
_The cloud runner — GitHub Actions / GitLab CI / Jenkins / similar. Gates merges and ships builds._
### Tasks
- Define when {check name?} runs (push, PR, tag)
- Pin runner image / version
- Source secrets from the runner's secret store (never in repo)
- Add {check name?} as a required status check in branch protection
- Publish build artefacts where downstream needs them
- Document failure triage steps in the repo README
### ACs
- {check name?} runs on every push and blocks merge on failure
- Green build produces the expected artefact

## deploy-base — Deploy Environment
_Stand up or refresh a deploy environment._
### Tasks
- Define infra as code for {environment name?}
- Source secrets from the secret store (never commit)
- Expose health-check endpoint
- Document rollback path
- Attach monitoring + alerts
- Review cost
### ACs
- {environment name?} reachable and healthy
- Rollback verified end-to-end

## monitor-base — Monitoring / Alert
_Wire up one alert._
### Tasks
- Pick SLI for {what are we alerting on?}
- Agree threshold with on-call
- Route alert to the on-call channel
- Link runbook from the alert
- Test by triggering a false positive
- Add entry to the monitoring index
### ACs
- {what are we alerting on?} pages on-call within minutes
- Alert auto-resolves when condition clears

## incident-base — Incident Postmortem
_Postmortem for one incident._
### Tasks
- Reconstruct timeline of {incident name or date?}
- Identify root cause(s)
- Quantify customer impact
- List contributing factors (no blame)
- Create + assign action items
- Circulate postmortem doc
### ACs
- Postmortem for {incident name or date?} published
- All action items have an owner and due date

## brand-base — Brand Asset
_Ship one brand asset._
### Tasks
- Sign off brief for {asset name?}
- Share drafts for review
- Incorporate revisions
- Export {asset name?} in all required formats / sizes
- File in the brand asset library
- Note usage guidelines
### ACs
- {asset name?} available in the brand library
- Required formats covered for {where will this be used?}

## research-base — Research / Spike
_Time-boxed research spike._
### Tasks
- Write the question(s) we're answering for {what are we trying to learn?}
- Choose method (desk, interview, prototype, data)
- Run the work within {timebox in days?} days
- Document findings
- Make recommendations
- Create follow-up stories where needed
### ACs
- {what are we trying to learn?} has a written answer
- Timebox respected

## refactor-base — Tech-Debt Refactor
_Bounded refactor of one area._
### Tasks
- Bound scope of refactor on {area to refactor?} (and what is NOT in scope)
- Keep behaviour-preserving (tests stay green)
- Add tests where coverage was missing
- Measure that performance did not regress
- Get review from an owner of {area to refactor?}
- Write migration notes if API changed
### ACs
- {area to refactor?} cleaner with no behaviour change
- Test coverage equal or better than before

## deps-base — Dependency Upgrade
_Upgrade one dependency._
### Tasks
- Review changelog / breaking changes for {dependency?} → {target version?}
- Update lockfile to {target version?}
- Run full test suite
- Smoke critical paths manually
- Address security advisories
- Note bundle / binary size impact
### ACs
- {dependency?} on {target version?} in production
- All tests green, no advisories outstanding

## docs-base — Documentation Page
_Write or refresh one doc page._
### Tasks
- State audience and scope for {doc topic?} up top
- Have steps verified by someone who didn't write them
- Make code samples copy-pasteable and tested
- Link from a discoverable index
- Refresh screenshots / diagrams
- List owner for future updates
### ACs
- {doc topic?} doc lets a reader {what should the reader be able to do?}
- Doc has a named owner

## onboarding-base — Onboarding Doc
_Onboarding for one role._
### Tasks
- Write day-0 setup steps for {role or team?}
- List access requests
- List people to meet
- Define first-week goals
- Add a where-to-ask-for-help section
- Have a recent joiner review it
### ACs
- New {role or team?} hire shippable by end of week 1
- Doc reviewed by someone onboarded in the last 90 days

## campaign-base — Marketing Campaign Asset Set
_Asset set for one campaign._
### Tasks
- Define brief and channel mix for {campaign name?}
- Approve hero asset
- Produce cut-downs for each channel
- Approve copy variants
- Define tracking links / UTM scheme
- Sign off launch checklist
### ACs
- {campaign name?} launches with all channels covered
- Tracking in place to measure {what does success look like?}

## social-base — Social Post Series
_Schedule one post series._
### Tasks
- Book calendar dates for {series name?}
- Draft and approve copy
- Attach visuals
- Define CTA per post
- Schedule in social tool
- Set reporting plan
### ACs
- {series name?} fully scheduled
- Reporting dashboard ready before first post goes live

## video-base — Video / Trailer
_Produce one video._
### Tasks
- Approve treatment for {video name?}
- Approve storyboard
- Share rough cut for notes
- Picture lock
- Lock sound mix
- Deliver final master in required formats
### ACs
- {video name?} delivered in all required formats
- Sells {what should the video sell?} clearly

