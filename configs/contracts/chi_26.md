Feature 3 adds request-dependent dynamic route selection.
Feature 4 adds explicit route priorities.
Preserve existing public APIs and default routing behavior, following the complete public feature requests and project conventions.
Both features may touch route lookup, context and handler selection; neither feature owns an entire module.
The fixed container supplies Go; use public or self-authored Go tests and report actual test outcomes.
Language-specific environment override: this is a Go repository. Use go test for public validation, including verify_integration package targets such as ["./..."], in place of the generic Python/pytest wording. Do not install Python test dependencies.
