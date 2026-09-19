# CFR M1 Integration Report

## Final M1 Host regression

RunId: `20260818T025807Z-728bd42e`
`GateOrigin=host_manual` / `RuntimeExecutionContext=HOST`

Network, Writer Handoff, Integration A–F, and BindingRecovery all passed. The final BindingRecovery evidence was:

```text
PostCTurnObservedOffset: 72800
PostCTurnPersistedOffset: 72800
ExpectedByteOffset: 72800
RecoveredByteOffset: 72800
ByteOffsetRecovered: true
RecoveryNoHistoricalReplay: true
ThreadResumeAfterRestart: true
SameThreadAfterRestart: true
```

`BlockingIssues=[]` and `M1Verdict=CFR_M1_CODEX_CORE_COMPLETE`.

## M1.1A regression contract

M1.1A must rerun the full Host command after boundary hardening:

```text
python .\scripts\run_m1_integration.py --timeout 90 --gate-origin host_manual
```

No M1 acceptance check may be removed, weakened, converted from FAIL to WARN, or skipped. A post-hardening M1 failure means M1.1A fails regardless of other local probes.
