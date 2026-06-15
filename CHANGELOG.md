# [0.7.0](https://github.com/Prog-Strength/prog-strength-developer/compare/v0.6.2...v0.7.0) (2026-06-15)


### Features

* **observability:** add section banners and human-readable run history to Developer Platform ([4da9ad7](https://github.com/Prog-Strength/prog-strength-developer/commit/4da9ad75806697405001e64ffb1bdc376e4a77be))

## [0.6.2](https://github.com/Prog-Strength/prog-strength-developer/compare/v0.6.1...v0.6.2) (2026-06-14)


### Bug Fixes

* **observability:** newline-terminate worker run-metric push ([f08561d](https://github.com/Prog-Strength/prog-strength-developer/commit/f08561d0339fe5a2fe1a2ccff737d978efec5563))

## [0.6.1](https://github.com/Prog-Strength/prog-strength-developer/compare/v0.6.0...v0.6.1) (2026-06-10)


### Bug Fixes

* **manager:** pre-create pushgateway data dir with non-root UID ([#11](https://github.com/Prog-Strength/prog-strength-developer/issues/11)) ([c6abfa2](https://github.com/Prog-Strength/prog-strength-developer/commit/c6abfa2fe5e73f7ff3bcd7ebc3b5505aef5e86e9)), closes [#8](https://github.com/Prog-Strength/prog-strength-developer/issues/8)

# [0.6.0](https://github.com/Prog-Strength/prog-strength-developer/compare/v0.5.0...v0.6.0) (2026-06-10)


### Features

* **observability:** ship Claude SDK debug logs and surface failure rate ([#10](https://github.com/Prog-Strength/prog-strength-developer/issues/10)) ([999fd9e](https://github.com/Prog-Strength/prog-strength-developer/commit/999fd9e64d0e2598028f4e0e724d793ab3983379))

# [0.5.0](https://github.com/Prog-Strength/prog-strength-developer/compare/v0.4.2...v0.5.0) (2026-06-10)


### Features

* **docs:** require Conventional Commits on PRs to drive semantic-release ([#9](https://github.com/Prog-Strength/prog-strength-developer/issues/9)) ([910d69d](https://github.com/Prog-Strength/prog-strength-developer/commit/910d69ddd7e23e917d104476cafac89780c72816)), closes [#8](https://github.com/Prog-Strength/prog-strength-developer/issues/8)

## [0.4.2](https://github.com/Prog-Strength/prog-strength-developer/compare/v0.4.1...v0.4.2) (2026-06-09)


### Bug Fixes

* **dashboards:** drop noise columns from Running workers table ([8f6e09b](https://github.com/Prog-Strength/prog-strength-developer/commit/8f6e09bd39f1c66f821dd7b0c9b8399f82407534))

## [0.4.1](https://github.com/Prog-Strength/prog-strength-developer/compare/v0.4.0...v0.4.1) (2026-06-09)


### Bug Fixes

* **dashboards:** render Running workers started_at as a date ([ef5049b](https://github.com/Prog-Strength/prog-strength-developer/commit/ef5049b84e48206120faaebeeba65f88691533f7))

# [0.4.0](https://github.com/Prog-Strength/prog-strength-developer/compare/v0.3.3...v0.4.0) (2026-06-09)


### Features

* **dashboards:** warning/critical threshold lines on CPU and memory ([46986bd](https://github.com/Prog-Strength/prog-strength-developer/commit/46986bdefe82abd8bf4c86f115bb4b1c6ad6f056))

## [0.3.3](https://github.com/Prog-Strength/prog-strength-developer/compare/v0.3.2...v0.3.3) (2026-06-09)


### Bug Fixes

* **manager:** pre-create per-service data dirs with container UIDs ([7fbb4dc](https://github.com/Prog-Strength/prog-strength-developer/commit/7fbb4dc8507da3bbe79e4fef0fa228e967a8ca97))

## [0.3.2](https://github.com/Prog-Strength/prog-strength-developer/compare/v0.3.1...v0.3.2) (2026-06-09)


### Bug Fixes

* **iam:** worker_inline TerminateSelf - drop unsupported policy variable ([ca61df3](https://github.com/Prog-Strength/prog-strength-developer/commit/ca61df30932ef1f3465d1dd6893a61ab9f27167d)), closes [#7](https://github.com/Prog-Strength/prog-strength-developer/issues/7) [pre-#7](https://github.com/pre-/issues/7)

## [0.3.1](https://github.com/Prog-Strength/prog-strength-developer/compare/v0.3.0...v0.3.1) (2026-06-09)


### Bug Fixes

* **iam:** grant GHA role permissions for manager + worker policy update ([3737acf](https://github.com/Prog-Strength/prog-strength-developer/commit/3737acf68d46826a006be02446403639d832f114)), closes [#7](https://github.com/Prog-Strength/prog-strength-developer/issues/7)

# [0.3.0](https://github.com/Prog-Strength/prog-strength-developer/compare/v0.2.0...v0.3.0) (2026-06-09)


### Features

* developer manager, concurrent workers, dashboards ([#7](https://github.com/Prog-Strength/prog-strength-developer/issues/7)) ([fc6a1a8](https://github.com/Prog-Strength/prog-strength-developer/commit/fc6a1a8618ce397c745bfb753332b9a82449bb76))

# [0.2.0](https://github.com/Prog-Strength/prog-strength-developer/compare/v0.1.0...v0.2.0) (2026-06-06)


### Features

* **prompt:** require a structured template for the docs status-flip PR ([#6](https://github.com/Prog-Strength/prog-strength-developer/issues/6)) ([6d7b797](https://github.com/Prog-Strength/prog-strength-developer/commit/6d7b797969c45a1d29243b41e3e8236a2fbb8ea6))

# [0.1.0](https://github.com/Prog-Strength/prog-strength-developer/compare/v0.0.0...v0.1.0) (2026-06-03)


### Features

* **ci:** automate terraform plan/apply and add semantic-release versioning ([#5](https://github.com/Prog-Strength/prog-strength-developer/issues/5)) ([c2d2fdd](https://github.com/Prog-Strength/prog-strength-developer/commit/c2d2fdd3e8ebe8cc749b6e22b11dbc957775b41e))
