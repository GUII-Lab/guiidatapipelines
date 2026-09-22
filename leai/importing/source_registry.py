"""Disposition of every concrete legacy datapipeline model.

This is import-planning authority. It does not authorize a row to be imported:
the offline importer must still prove exact ownership, scope, consent, and
foreign-key closure or record a quarantined per-row outcome.
"""

SOURCE_MODEL_DISPOSITIONS = {
    "User": {"disposition": "exclude", "target": "none", "reason": "Legacy StudyCrafter user; never an LEAI instructor or student identity."},
    "Message": {"disposition": "exclude", "target": "none", "reason": "Legacy StudyCrafter message outside the LEAI response graph."},
    "FeedbackMessage": {"disposition": "transform", "target": "ResponseSession + ResponseMessage", "reason": "Map only exact survey-scoped anonymous sessions; quarantine unresolved linkage or consent."},
    "Course": {"disposition": "transform", "target": "Course", "reason": "Preserve approved course ownership and explicit settings after source-ID review."},
    "BannerAssignment": {"disposition": "quarantine", "target": "approved course experiment only", "reason": "Exposure records require separate experiment authorization and exact session scope."},
    "SessionIdentity": {"disposition": "quarantine", "target": "none", "reason": "Device keys and fingerprints are prohibited from canonical LEAI runtime."},
    "FeedbackGPT": {"disposition": "transform", "target": "QuestionSetRevision + SurveyOccurrence", "reason": "Freeze survey protocol and map exact course and published occurrence."},
    "SurveyCompletionCertificate": {"disposition": "transform", "target": "ResponseSession completion", "reason": "Preserve only when exact survey/session/output eligibility can be proven."},
    "FormSchema": {"disposition": "transform", "target": "QuestionSetRevision or QuestionSetTemplateRevision", "reason": "Freeze one exact structured protocol, not a parallel mutable authoring source."},
    "TeamConfiguration": {"disposition": "transform", "target": "TeamConfiguration", "reason": "Preserve course-owned configuration after exact course mapping."},
    "Team": {"disposition": "transform", "target": "TeamDefinition", "reason": "Preserve definitions under mapped course-owned configuration."},
    "SurveyTeamSnapshot": {"disposition": "transform", "target": "TeamSnapshot", "reason": "Freeze occurrence-owned historical snapshot without legacy resync."},
    "SurveyTeam": {"disposition": "transform", "target": "TeamSnapshotItem", "reason": "Preserve team labels only under an exact frozen occurrence snapshot."},
    "SessionTeamAssignment": {"disposition": "transform", "target": "ResponseSession team_snapshot_item", "reason": "Require exact anonymous session and same-occurrence team ownership."},
    "CustomGPT": {"disposition": "exclude", "target": "none", "reason": "Unrelated GPT configuration outside canonical LEAI authoring."},
    "FireData": {"disposition": "exclude", "target": "none", "reason": "Unrelated application data."},
    "Image": {"disposition": "exclude", "target": "none", "reason": "Unrelated uploaded image assets; no student blob import."},
    "LEAIChatSession": {"disposition": "transform", "target": "AnalysisChatSession + AnalysisScopeOccurrence", "reason": "Require exact course, actor, and normalized occurrence scope; quarantine unresolved actors/scopes."},
    "LEAIChatMessage": {"disposition": "transform", "target": "AnalysisChatMessage + AnalysisCitation", "reason": "Preserve only messages and citations with exact chat and response-source linkage."},
    "LEAIQuickTake": {"disposition": "regenerate", "target": "AnalysisSnapshot", "reason": "Disposable derived cache must be regenerated from canonical responses."},
    "LEAIPdfIngestBatch": {"disposition": "transform", "target": "PdfImportBatch", "reason": "Preserve committed manifest under exact survey and account provenance."},
    "LEAIPdfIngestJob": {"disposition": "exclude", "target": "none", "reason": "Transient active preview job; restart in target environment if needed."},
}
