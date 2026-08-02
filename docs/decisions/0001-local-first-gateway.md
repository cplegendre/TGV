# ADR 0001: Local-first gateway

- Status: Accepted
- Date: 2026-07-27

## Context

Simple repository questions should not require a frontier-model session, while advanced tasks still benefit from stronger hosted reasoning.

## Decision

TGV will be a local orchestrator that can answer locally, prepare context, or escalate. MCP provides standardized tools but is not the top-level routing mechanism.

## Consequences

The CLI must own routing and policy. Hosted agents may call TGV over MCP, but that secondary integration does not replace the local-first entry point.
