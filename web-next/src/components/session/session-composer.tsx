"use client"

import * as React from "react"
import { ArrowUp, Check, ChevronDown, Loader2, Square } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import {
  AttachmentButton,
  AttachmentPreviewList,
  DragOverlay,
  useAttachments,
  type AttachedFile,
} from "@/components/attachment-input"
import { cn } from "@/lib/utils"
import type {
  ProtocolCapabilitySet,
  ProtocolModelCatalog,
  ProtocolPermissionCatalog,
  RuntimeCommand,
  RuntimeStatusValue,
  SessionRuntimeState,
  SessionView,
} from "@/features/dashboard/types"
import { useTranslations } from "next-intl"
import {
  catalogItemDisabledReason,
  catalogItemEnabled,
  catalogI18nText,
  modelCatalogDisplayName,
  modelIdsForSelectionId,
  permissionIdForSelectionId,
  selectionIdForModelCatalog,
  selectionIdForPermissionCatalog,
} from "@/components/session/catalog-selection"
import { SelectionSettingsDrawer } from "@/components/session/selection-settings-drawer"
import { CAPABILITY, capabilityIsUsable, findCapability, attachmentMimeTypes } from "@/components/session/capabilities"
import { useElementWidth } from "@/hooks/use-element-width"
import { sessionRuntimeId, sessionRuntimeType } from "@/features/dashboard/runtime-instances"
import { commandActionReason, commandAllowed, commandRequest, commandUi, exactCommand, parseSlashIntent, type CommandOutcome } from "@/components/session/runtime-command-model"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { MarkdownText } from "@/components/markdown-text"
import type { SteerOutcome } from "@/components/session/session-steer-result"

export type { AttachedFile }

type ExplicitSelection = {
  id: string
  state: "pending" | "accepted" | "rejected"
  observed: boolean
  previous: ExplicitSelection | null
}

function observeSelection(intent: ExplicitSelection | null, selectionId: string | null | undefined): void {
  for (let current = intent; current; current = current.previous) {
    if (selectionId === current.id) current.observed = true
  }
}

function currentSelectionIntent(intent: ExplicitSelection | null): ExplicitSelection | null {
  let current = intent
  while (current?.state === "rejected") current = current.previous
  return current?.state === "accepted" && current.observed ? null : current
}

export function SessionComposer({
  token,
  session,
  runtimeState,
  runtimeSyncPending = false,
  pendingInteractionCount,
  creatingSession = false,
  sending,
  interrupting,
  takeoverBusy,
  value,
  effectiveCapabilities,
  modelCatalog,
  permissionCatalog,
  runtimeCommands,
  commandsLoading = false,
  commandsError = false,
  stopOutcome = null,
  onCommandQueryChange,
  onValueChange,
  onSelectionChange,
  onSend,
  onSteer,
  onInterrupt,
  onCommand,
  onToggleTakeover,
}: {
  token: string
  session: SessionView
  runtimeState?: SessionRuntimeState | null
  runtimeSyncPending?: boolean
  pendingInteractionCount: number
  creatingSession?: boolean
  sending: boolean
  interrupting: boolean
  takeoverBusy: boolean
  value: string
  effectiveCapabilities: ProtocolCapabilitySet | null
  modelCatalog: ProtocolModelCatalog | null
  permissionCatalog: ProtocolPermissionCatalog | null
  runtimeCommands: RuntimeCommand[]
  commandsLoading?: boolean
  commandsError?: boolean
  stopOutcome?: { ok: boolean; message: string } | null
  onCommandQueryChange: (query: string | null) => void
  onValueChange: (value: string) => void
  onSelectionChange: (selections: { model?: string; permission?: string }) => Promise<boolean>
  onSend: (
    content: string,
    attachments: AttachedFile[],
    selections: { model?: string; permission?: string },
  ) => Promise<boolean>
  onSteer?: (content: string, attachments: AttachedFile[]) => Promise<SteerOutcome>
  onInterrupt: () => void
  onCommand: (command: string, options: { args: string[]; raw: string }) => Promise<CommandOutcome>
  onToggleTakeover: () => void
}) {
  const tSession = useTranslations("dashboard.session")
  const tNew = useTranslations("dashboard.new")
  const composerRef = React.useRef<HTMLDivElement | null>(null)
  const textareaRef = React.useRef<HTMLTextAreaElement | null>(null)
  const valueRef = React.useRef(value)
  valueRef.current = value
  const pendingCommandRef = React.useRef(false)
  const pendingMessageRef = React.useRef(false)
  const sessionVisitRef = React.useRef({ id: session.id, sequence: 0 })
  if (sessionVisitRef.current.id !== session.id) {
    sessionVisitRef.current = { id: session.id, sequence: sessionVisitRef.current.sequence + 1 }
  }
  const requestSequenceRef = React.useRef(0)
  const activeCommandRequestRef = React.useRef<number | null>(null)
  const activeMessageRequestRef = React.useRef<number | null>(null)
  const pendingSteerRef = React.useRef(false)
  const activeSteerRequestRef = React.useRef<number | null>(null)
  const [steerPending, setSteerPending] = React.useState(false)
  const [messagePending, setMessagePending] = React.useState(false)
  const [steerFeedback, setSteerFeedback] = React.useState<SteerOutcome | null>(null)
  const [commandPending, setCommandPending] = React.useState(false)
  const [commandFeedback, setCommandFeedback] = React.useState<CommandOutcome | null>(null)
  const [commandInputError, setCommandInputError] = React.useState<string | null>(null)
  const [resultExpanded, setResultExpanded] = React.useState(false)
  const [selectorRequest, setSelectorRequest] = React.useState(0)
  React.useEffect(() => {
    setCommandFeedback(null)
    setCommandInputError(null)
    setCommandPending(false)
    pendingCommandRef.current = false
    pendingMessageRef.current = false
    activeCommandRequestRef.current = null
    activeMessageRequestRef.current = null
    setMessagePending(false)
    pendingSteerRef.current = false
    activeSteerRequestRef.current = null
    setSteerPending(false)
    setSteerFeedback(null)
  }, [session.id])
  const composerWidth = useElementWidth(composerRef)
  const runtimeStatus = effectiveRuntimeStatus(runtimeState, session)
  const runtimeSelections = runtimeState?.selections ?? {}
  const dsh = sessionRuntimeType(session) === "dsh"
  const actualModel = runtimeState?.metadata.modelSelection as { provider?: string; model?: string; reasoningEffort?: string } | undefined
  const actualPermission = runtimeState?.metadata.permissionPreset as { id?: string; name?: string } | undefined
  const runtimeScope = {
    runtimeId: sessionRuntimeId(session),
    runtimeType: sessionRuntimeType(session),
  }
  const isRunning = runtimeStatus === "running"
  const isWaitingApproval = runtimeStatus === "waiting_approval"
  const isBlocked = runtimeStatus === "blocked"
  const isStopping = runtimeStatus === "stopping"
  const isWaiting = runtimeStatus === "waiting" || runtimeStatus === "pending"
  const isError = runtimeStatus === "error"
  const isDisconnected = runtimeStatus === "disconnected"
  const sourceUnavailable = session.archived || runtimeSyncPending
  const connectorOnline = session.connectorStatus === "online"
  const acceptsUserInput =
    connectorOnline &&
    !sourceUnavailable &&
    !isDisconnected &&
    !isWaiting &&
    !isRunning &&
    !isStopping &&
    !isWaitingApproval &&
    !isBlocked
  const canUseSendMessage = capabilityIsUsable(effectiveCapabilities, CAPABILITY.sendMessage, runtimeScope)
  const canUseSteer = capabilityIsUsable(effectiveCapabilities, CAPABILITY.steer, runtimeScope)
  const canUseInterrupt = capabilityIsUsable(effectiveCapabilities, CAPABILITY.interrupt, runtimeScope)
  const canUseCommands = capabilityIsUsable(effectiveCapabilities, CAPABILITY.commands, runtimeScope)
  const interruptCapability = findCapability(effectiveCapabilities, CAPABILITY.interrupt, runtimeScope)
  const canUseModelCatalog = capabilityIsUsable(effectiveCapabilities, CAPABILITY.modelCatalog, runtimeScope)
  const canUsePermissionCatalog = capabilityIsUsable(
    effectiveCapabilities,
    CAPABILITY.permissionCatalog,
    runtimeScope,
  )
  const canUseEffortCatalog = capabilityIsUsable(effectiveCapabilities, CAPABILITY.effortCatalog, runtimeScope)
  const canUseAttachments = capabilityIsUsable(effectiveCapabilities, CAPABILITY.attachment, runtimeScope)
  const allowedMimeTypes = React.useMemo(() => attachmentMimeTypes(effectiveCapabilities, runtimeScope), [effectiveCapabilities, runtimeScope])
  const {
    attachments,
    attachmentsAllowed,
    attachmentError,
    isDragging,
    uploadsPending,
    uploadFailed,
    allUploaded,
    add,
    remove,
    clear,
    clearIfUnchanged,
    restoreIfEmpty,
    onDragEnter,
    onDragLeave,
    onDragOver,
    onDrop,
  } = useAttachments({ sessionId: creatingSession ? undefined : session.id, token, enabled: canUseAttachments, allowedMimeTypes })
  const canSend =
    canUseSendMessage &&
    !creatingSession &&
    !sending &&
    !messagePending &&
    !steerPending &&
    !pendingSteerRef.current &&
    !interrupting &&
    acceptsUserInput
  const commandWritable = !creatingSession && !sending && !messagePending && !interrupting && !steerPending && !commandPending && session.takeover && !sourceUnavailable
  const hasInput = value.trim().length > 0 || attachments.length > 0
  const attachmentsReady = attachmentsAllowed && (attachments.length === 0 || (allUploaded && !uploadsPending && !uploadFailed))
  const canSteer = Boolean(onSteer && canUseSteer && isRunning && connectorOnline && session.takeover &&
    !creatingSession && !sourceUnavailable && pendingInteractionCount === 0 &&
    !sending && !messagePending && !interrupting && !pendingSteerRef.current && !steerPending)
  const activeSessionCanInterrupt = Boolean(
    connectorOnline &&
    interruptCapability?.supported &&
    interruptCapability.allowed &&
    ((isWaiting || isRunning || isStopping || isWaitingApproval || isBlocked) || (
      runtimeState?.metadata.codexPresentation &&
      typeof runtimeState.metadata.codexPresentation === "object" &&
      (runtimeState.metadata.codexCoordination as {available?:boolean} | undefined)?.available !== false &&
      (runtimeState.metadata.codexPresentation as {threadGoal?: {status?: string}}).threadGoal?.status === "active" &&
      (runtimeState.metadata.codexCapabilities as {userSessionStop?: boolean} | undefined)?.userSessionStop === true
    )),
  )
  const showInterrupt = !creatingSession && canUseInterrupt && activeSessionCanInterrupt
  const [selectedPermissionMode, setSelectedPermissionMode] = React.useState("")
  const [selectedModel, setSelectedModel] = React.useState("")
  const [selectedReasoning, setSelectedReasoning] = React.useState("")
  const [selectionIntentRevision, setSelectionIntentRevision] = React.useState(0)
  const explicitSelectionRef = React.useRef<{ model: ExplicitSelection | null; permission: ExplicitSelection | null }>({ model: null, permission: null })
  React.useEffect(() => {
    explicitSelectionRef.current = { model: null, permission: null }
    setSelectedPermissionMode("")
    setSelectedModel("")
    setSelectedReasoning("")
  }, [session.id])
  const permissionItems = permissionCatalog?.permissions.map((item) => ({
    id: item.id,
    label: catalogI18nText(tNew, item.metadata, "labelKey", item.displayName),
    description: catalogI18nText(tNew, item.metadata, "descriptionKey", item.description),
    default: item.default,
    enabled: catalogItemEnabled(item),
    disabledReason: catalogItemDisabledReason(item),
    selectionId: item.selectionId,
  })) ?? []
  const modelItems = modelCatalog?.models.map((item) => ({
    id: item.id,
    label: modelCatalogDisplayName(
      item,
      modelCatalog.models,
      catalogI18nText(tNew, item.metadata, "labelKey", item.displayName),
      tNew("defaultReasoning"),
    ),
    default: item.default,
    enabled: catalogItemEnabled(item),
    disabledReason: catalogItemDisabledReason(item),
    selectionId: item.selectionId,
    reasoningItems: item.reasoningItems.map((reasoning) => ({
      id: reasoning.id,
      label: catalogI18nText(tNew, reasoning.metadata, "labelKey", reasoning.displayName),
      default: reasoning.default,
      enabled: catalogItemEnabled(reasoning),
      disabledReason: catalogItemDisabledReason(reasoning),
      selectionId: reasoning.selectionId,
    })),
  })) ?? []
  const modelObservedOrChosen = dsh || Boolean(runtimeSelections.model || explicitSelectionRef.current.model)
  const permissionObservedOrChosen = dsh || Boolean(runtimeSelections.permission || explicitSelectionRef.current.permission)
  const selectedModelItem = modelObservedOrChosen ? modelItems.find((item) => item.id === selectedModel) : undefined
  const effortItems = selectedModelItem?.reasoningItems ?? []
  const modelSelectionValue = modelIdsForSelectionId(modelCatalog, runtimeSelections.model ?? null, dsh)
  const permissionSelectionValue = permissionIdForSelectionId(permissionCatalog, runtimeSelections.permission ?? null, dsh)
  const permissionValue = permissionSelectionValue
  const modelValue = modelSelectionValue?.modelId ?? ""
  const effortValue = modelSelectionValue?.reasoningId ?? ""
  const permissionLabel =
    (permissionObservedOrChosen ? permissionItems.find((item) => item.id === selectedPermissionMode)?.label : null) ?? (dsh ? actualPermission?.name : null) ?? tSession("currentSettingUnknown")
  const modelLabel = selectedModelItem?.label ?? (dsh && actualModel?.model ? `${actualModel.model}（${actualModel.provider}）` : tSession("currentSettingUnknown"))
  const effortLabel = effortItems.find((item) => item.id === selectedReasoning)?.label ?? (dsh ? actualModel?.reasoningEffort : null) ?? tNew("reasoning")
  const hasSelectors = Boolean(permissionItems.length > 0 || modelItems.length > 0)
  const compactSelectors = hasSelectors && ((composerWidth > 0 && composerWidth < 560) || selectorRequest > 0)
  const permissionSelectorDisabled = creatingSession || sourceUnavailable || !connectorOnline || sending || messagePending || steerPending || !canUsePermissionCatalog
  const modelSelectorDisabled = creatingSession || sourceUnavailable || !connectorOnline || sending || messagePending || steerPending || !canUseModelCatalog
  const effortSelectorDisabled = creatingSession || sourceUnavailable || !connectorOnline || sending || messagePending || steerPending || !canUseEffortCatalog
  const selectorsDisabled = permissionSelectorDisabled && modelSelectorDisabled

  React.useEffect(() => {
    if (dsh) { setSelectedPermissionMode(permissionValue); return }
    observeSelection(explicitSelectionRef.current.permission, runtimeSelections.permission)
    const retained = currentSelectionIntent(explicitSelectionRef.current.permission)
    explicitSelectionRef.current.permission = retained
    if (retained) {
      setSelectedPermissionMode(permissionItems.find((item) => item.selectionId === retained.id && item.enabled)?.id ?? "")
      return
    }
    const hasRuntimePermission = permissionItems.some((item) => item.id === permissionValue && item.enabled)
    const nextPermission = hasRuntimePermission ? permissionValue : ""
    setSelectedPermissionMode(nextPermission)
  }, [dsh, permissionItems, permissionValue, runtimeSelections.permission, selectionIntentRevision])

  React.useEffect(() => {
    if (dsh) { setSelectedModel(modelValue); return }
    observeSelection(explicitSelectionRef.current.model, runtimeSelections.model)
    const retained = currentSelectionIntent(explicitSelectionRef.current.model)
    explicitSelectionRef.current.model = retained
    if (retained) {
      setSelectedModel(modelIdsForSelectionId(modelCatalog, retained.id)?.modelId ?? "")
      return
    }
    const hasRuntimeModel = modelItems.some((item) => item.id === modelValue && item.enabled)
    setSelectedModel(hasRuntimeModel ? modelValue : "")
  }, [dsh, modelCatalog, modelItems, modelValue, selectionIntentRevision])

  React.useEffect(() => {
    if (dsh) { setSelectedReasoning(effortValue); return }
    const explicit = explicitSelectionRef.current.model
    if (explicit) {
      setSelectedReasoning(modelIdsForSelectionId(modelCatalog, explicit.id)?.reasoningId ?? "")
      return
    }
    if (!runtimeSelections.model) { setSelectedReasoning(""); return }
    const hasRuntimeEffort = effortItems.some((item) => item.id === effortValue && item.enabled)
    const nextEffort = hasRuntimeEffort
      ? effortValue
      : effortItems.find((item) => item.default && item.enabled)?.id
        ?? effortItems.find((item) => item.enabled)?.id
        ?? ""
    setSelectedReasoning((current) =>
      hasRuntimeEffort || !current || !effortItems.some((item) => item.id === current && item.enabled) ? nextEffort : current,
    )
  }, [dsh, effortItems, effortValue, modelCatalog, runtimeSelections.model, selectionIntentRevision])
  const selectedModelSelection = modelObservedOrChosen
    ? selectionIdForModelCatalog(modelCatalog, selectedModel, selectedReasoning) ?? (dsh ? runtimeSelections.model : null)
    : null
  const selectedPermissionSelection = permissionObservedOrChosen
    ? selectionIdForPermissionCatalog(permissionCatalog, selectedPermissionMode) ?? (dsh && actualPermission?.id !== 'custom' ? runtimeSelections.permission : null)
    : null
  const choosePermission = (permissionId: string) => {
    if (permissionId === selectedPermissionMode && permissionObservedOrChosen) return
    const previousIntent = explicitSelectionRef.current.permission
    const nextSelection = selectionIdForPermissionCatalog(permissionCatalog, permissionId)
    if (!nextSelection) return
    const visit = sessionVisitRef.current
    const intent: ExplicitSelection = { id: nextSelection, state: "pending", observed: false, previous: previousIntent }
    explicitSelectionRef.current.permission = intent
    setSelectedPermissionMode(permissionId)
    void onSelectionChange({ permission: nextSelection }).then((ok) => {
      if (dsh || sessionVisitRef.current !== visit) return
      intent.state = ok ? "accepted" : "rejected"
      if (explicitSelectionRef.current.permission !== intent) return
      explicitSelectionRef.current.permission = currentSelectionIntent(intent)
      setSelectionIntentRevision((current) => current + 1)
    })
  }
  const chooseModel = (modelId: string, reasoningId: string) => {
    if (modelId === selectedModel && reasoningId === selectedReasoning && modelObservedOrChosen) return
    const previousIntent = explicitSelectionRef.current.model
    const nextSelection = selectionIdForModelCatalog(modelCatalog, modelId, reasoningId)
    if (!nextSelection) return
    const visit = sessionVisitRef.current
    const intent: ExplicitSelection = { id: nextSelection, state: "pending", observed: false, previous: previousIntent }
    explicitSelectionRef.current.model = intent
    setSelectedModel(modelId)
    setSelectedReasoning(reasoningId)
    void onSelectionChange({ model: nextSelection }).then((ok) => {
      if (dsh || sessionVisitRef.current !== visit) return
      intent.state = ok ? "accepted" : "rejected"
      if (explicitSelectionRef.current.model !== intent) return
      explicitSelectionRef.current.model = currentSelectionIntent(intent)
      setSelectionIntentRevision((current) => current + 1)
    })
  }
  const placeholder = creatingSession
    ? tSession("creatingPlaceholder")
    : runtimeSyncPending
      ? tSession("syncingSessionPlaceholder")
    : sourceUnavailable
      ? tSession("sourceUnavailablePlaceholder")
    : !session.takeover
    ? tSession("readOnlyPlaceholder")
    : isDisconnected || !connectorOnline
      ? tSession("deviceOfflinePlaceholder")
      : pendingInteractionCount > 0
        ? tSession("waitingApprovalPlaceholder")
        : isWaiting
          ? tSession("pendingPlaceholder")
          : isStopping || isRunning
            ? isRunning && canSteer ? tSession("steerPlaceholder") : tSession("busyPlaceholder")
            : isWaitingApproval || isBlocked
              ? tSession("waitingApprovalPlaceholder")
              : isError
                ? tSession("errorPlaceholder")
                : tSession("replyPlaceholder")
  const slashIntent = parseSlashIntent(value)
  const commandQuery = slashIntent?.command ?? null
  const showCommandMenu = commandQuery !== null && !slashIntent?.multiline && !slashIntent?.suffix.trim() && attachments.length === 0
  const commandSuggestions = React.useMemo(
    () => runtimeCommands.filter((command) => commandMatchesQuery(command, commandQuery)),
    [commandQuery, runtimeCommands],
  )
  React.useEffect(() => {
    onCommandQueryChange(slashIntent ? commandQuery : null)
  }, [commandQuery, onCommandQueryChange, slashIntent !== null])
  const canSubmitCommand = Boolean(slashIntent && !pendingCommandRef.current && commandWritable)
  const canSubmitMessage =
    canSend &&
    session.takeover &&
    hasInput &&
    attachmentsReady &&
    (attachments.length === 0 || canUseAttachments) && !slashIntent
  const canSubmitSteer = canSteer && value.trim().length > 0 && attachmentsReady &&
    (attachments.length === 0 || canUseAttachments) && !slashIntent
  const concurrentWriter = runtimeState?.error?.code === "DSH_CONCURRENT_WRITER_DETECTED"
  const updateValue = React.useCallback((nextValue: string) => {
    valueRef.current = nextValue
    onValueChange(nextValue)
    setCommandInputError(null)
    setSteerFeedback(null)
  }, [onValueChange])

  const runCommand = async (command: RuntimeCommand, raw: string, sourceDraft = raw) => {
    if (pendingCommandRef.current) return
    const intent = parseSlashIntent(raw)
    const actionReason = intent ? commandActionReason(intent, command) : null
    if (actionReason) {
      setCommandInputError(tSession.has(`commandReason_${actionReason}`) ? tSession(`commandReason_${actionReason}`) : actionReason)
      return
    }
    if (!commandAllowed(command, runtimeStatus, canUseCommands, commandWritable, connectorOnline)) {
      setCommandInputError(command.disabledReason && tSession.has(`commandReason_${command.disabledReason}`)
        ? tSession(`commandReason_${command.disabledReason}`) : command.disabledReason || tSession("commandUnavailable"))
      return
    }
    if (attachments.length) { setCommandInputError(tSession("commandAttachments")); return }
    const request = commandRequest(intent, command)
    if (!request) { setCommandInputError(tSession(intent?.multiline ? "commandMultiline" : "commandInvalidArgs")); return }
    const ui = commandUi(command)
    if (ui?.kind === "selector") {
      // The existing settings drawer owns model, reasoning and permission selection.
      const available = ui.target === "model" ? modelItems.length > 0 && !modelSelectorDisabled
        : ui.target === "reasoning" ? effortItems.length > 0 && !effortSelectorDisabled
        : ui.target === "permission" ? permissionItems.length > 0 && !permissionSelectorDisabled : false
      if (!available) {
        setCommandInputError(tSession("commandUnavailable"))
        return
      }
      setSelectorRequest((current) => current + 1)
      return
    }
    const submittedVisit = sessionVisitRef.current
    const requestId = ++requestSequenceRef.current
    activeCommandRequestRef.current = requestId
    const isCurrentRequest = () => sessionVisitRef.current === submittedVisit && activeCommandRequestRef.current === requestId
    pendingCommandRef.current = true
    setCommandPending(true)
    setCommandFeedback(null)
    try {
      const outcome = await onCommand(command.id, { args: request.args, raw: request.raw })
      if (!isCurrentRequest()) return
      setCommandFeedback(outcome)
      setResultExpanded(false)
      if (outcome.ok && valueRef.current === sourceDraft) updateValue("")
    } catch (error) {
      if (isCurrentRequest()) {
        setCommandFeedback({ok:false,state:"unknown",code:"command_outcome_unknown",message:error instanceof Error ? error.message : tSession("commandFailed"),result:null})
      }
    } finally {
      if (isCurrentRequest()) {
        activeCommandRequestRef.current = null
        pendingCommandRef.current = false
        setCommandPending(false)
      }
    }
  }

  const runSteer = async () => {
    if (!canSubmitSteer || pendingSteerRef.current || !onSteer) return
    const text = value
    const files = attachments
    const visit = sessionVisitRef.current
    const requestId = ++requestSequenceRef.current
    activeSteerRequestRef.current = requestId
    const isCurrentRequest = () => sessionVisitRef.current === visit && activeSteerRequestRef.current === requestId
    pendingSteerRef.current = true
    setSteerPending(true)
    setSteerFeedback(null)
    try {
      const outcome = await onSteer(text, files)
      if (!isCurrentRequest()) return
      if (outcome.ok && outcome.state === "accepted") {
        if (valueRef.current === text && clearIfUnchanged(files)) updateValue("")
      } else {
        setSteerFeedback(outcome)
      }
    } catch (error) {
      if (isCurrentRequest()) setSteerFeedback({ ok: false, state: "unknown", message: error instanceof Error ? error.message : null })
    } finally {
      if (isCurrentRequest()) {
        activeSteerRequestRef.current = null
        pendingSteerRef.current = false
        setSteerPending(false)
      }
    }
  }

  const submit = async () => {
    if (!hasInput) return
    if (slashIntent) {
      if (attachments.length) { setCommandInputError(tSession("commandAttachments")); return }
      if (commandsLoading) { setCommandInputError(tSession("commandLoading")); return }
      const command = exactCommand(slashIntent, runtimeCommands)
      if (!command) { setCommandInputError(commandsError ? tSession("commandCatalogError") : tSession("commandUnknown")); return }
      await runCommand(command, value)
      return
    }
    if (isRunning) {
      await runSteer()
      return
    }
    if (!canSubmitMessage || pendingMessageRef.current || pendingSteerRef.current) return
    const text = value
    const files = attachments
    const visit = sessionVisitRef.current
    const requestId = ++requestSequenceRef.current
    activeMessageRequestRef.current = requestId
    const isCurrentRequest = () => sessionVisitRef.current === visit && activeMessageRequestRef.current === requestId
    pendingMessageRef.current = true
    setMessagePending(true)
    updateValue("")
    clear({ revokePreviews: false })
    try {
      const sent = await onSend(text, files, {
        ...(selectedModelSelection ? { model: selectedModelSelection } : {}),
        ...(selectedPermissionSelection ? { permission: selectedPermissionSelection } : {}),
      })
      if (!sent && isCurrentRequest() && valueRef.current === "") {
        updateValue(text)
        restoreIfEmpty(files)
      }
    } catch {
      if (isCurrentRequest() && valueRef.current === "") {
        updateValue(text)
        restoreIfEmpty(files)
      }
    } finally {
      if (isCurrentRequest()) {
        activeMessageRequestRef.current = null
        pendingMessageRef.current = false
        setMessagePending(false)
      }
    }
  }

  return (
    <div
      className="shrink-0 px-4 pb-4 pt-2"
      onDragEnter={onDragEnter}
      onDragLeave={onDragLeave}
      onDragOver={onDragOver}
      onDrop={onDrop}
    >
      <DragOverlay isDragging={isDragging} />
      <div className="mx-auto w-full max-w-3xl space-y-2">
        {stopOutcome ? <Alert variant={stopOutcome.ok ? "default" : "destructive"} role={stopOutcome.ok ? "status" : "alert"}><AlertDescription>{stopOutcome.message}</AlertDescription></Alert> : null}
        {steerFeedback ? <Alert variant="destructive" role="alert"><AlertDescription>
          {tSession(steerFeedback.state === "unknown" ? "steerUnknownOutcome" : "steerRejected")}
          {steerFeedback.message ? <span> {steerFeedback.message}</span> : null}
        </AlertDescription></Alert> : null}
        {commandFeedback ? <Alert variant={commandFeedback.ok ? "default" : "destructive"} role={commandFeedback.ok ? "status" : "alert"}>
          <AlertDescription>
            <span>{tSession(commandFeedback.ok ? (commandFeedback.state === "completed" ? "commandCompleted" : "commandAccepted") : commandFeedback.state === "unknown" ? "commandUnknownOutcome" : "commandFailed")}</span>
            {commandFeedback.message ? <div className="mt-1"><MarkdownText text={commandFeedback.message.length > 500 && !resultExpanded ? `${commandFeedback.message.slice(0,500)}…` : commandFeedback.message} /></div> : null}
            {commandFeedback.message && commandFeedback.message.length > 500 ? <Button type="button" variant="ghost" size="xs" onClick={() => setResultExpanded(!resultExpanded)}>{tSession(resultExpanded ? "goalLess" : "goalDetails")}</Button> : null}
          </AlertDescription>
        </Alert> : null}
        {concurrentWriter ? (
          <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
            {tSession("dshConcurrentWriter")}
          </div>
        ) : null}
        <div
          ref={composerRef}
          className={cn(
            "relative rounded-2xl border border-border bg-card/85 shadow-sm backdrop-blur-xl transition-colors supports-backdrop-filter:bg-card/70 focus-within:border-ring focus-within:ring-2 focus-within:ring-ring/20",
            isDragging && "border-primary bg-primary/5",
          )}
        >
          {isDragging ? (
            <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-2xl bg-background/75 text-sm font-medium text-foreground backdrop-blur-sm">
              {tSession("dropFiles")}
            </div>
          ) : null}
          <div className="space-y-3 px-4 pt-4">
            <AttachmentPreviewList attachments={attachments} onRemove={remove} />
            {attachmentError ? <p role="alert" className="text-xs text-destructive">{attachmentError}</p> : null}
            {commandInputError ? <p role="alert" className="text-xs text-destructive">{commandInputError}</p> : null}
            {showCommandMenu ? (
              <div className="rounded-xl border border-border bg-popover p-1 text-sm shadow-sm">
                {commandSuggestions.length > 0 ? (
                  commandSuggestions.map((command) => (
                    <button
                      key={command.id}
                      type="button"
                      className={cn(
                        "flex w-full items-start gap-3 rounded-lg px-3 py-2 text-left transition-colors hover:bg-accent hover:text-accent-foreground",
                        !command.enabled && "cursor-not-allowed opacity-50 hover:bg-transparent hover:text-current",
                      )}
                      disabled={!commandAllowed(command, runtimeStatus, canUseCommands, commandWritable, connectorOnline)}
                      onClick={() => {
                        if (!commandAllowed(command, runtimeStatus, canUseCommands, commandWritable, connectorOnline)) return
                        const sourceDraft = valueRef.current
                        const resolved = slashIntent && exactCommand(slashIntent, [command]) === command
                        const raw = resolved ? sourceDraft : `/${command.id}`
                        if (command.acceptsArgs) {
                          updateValue(raw === `/${command.id}` ? `${raw} ` : raw)
                          textareaRef.current?.focus()
                          return
                        }
                        void runCommand(command, raw, sourceDraft)
                      }}
                    >
                      <span className="code-mono shrink-0 text-xs text-primary">/{command.id}</span>
                      <span className="min-w-0">
                        <span className="block font-medium">{command.title}</span>
                        <span className="block text-xs text-muted-foreground">
                          {command.disabledReason ? (tSession.has(`commandReason_${command.disabledReason}`) ? tSession(`commandReason_${command.disabledReason}`) : command.disabledReason) : commandUi(command)?.kind === "execute" ? (commandUi(command) as {argumentHint?:string}).argumentHint || command.description : command.description}
                        </span>
                      </span>
                    </button>
                  ))
                ) : commandsLoading ? (
                  <div className="px-3 py-2 text-xs text-muted-foreground">{tSession("commandLoading")}</div>
                ) : commandsError ? (
                  <div role="alert" className="px-3 py-2 text-xs text-destructive">{tSession("commandCatalogError")}</div>
                ) : !canUseCommands ? (
                  <div className="px-3 py-2 text-xs text-muted-foreground">{tSession("commandUnavailable")}</div>
                ) : (
                  <div className="px-3 py-2 text-xs text-muted-foreground">{tSession("commandNoMatches")}</div>
                )}
              </div>
            ) : null}
            <Textarea
              ref={textareaRef}
              value={value}
              onChange={(event) => updateValue(event.currentTarget.value)}
              onKeyDown={(event) => {
                if (event.nativeEvent.isComposing) return
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault()
                  void submit()
                }
              }}
              placeholder={placeholder}
              disabled={!connectorOnline || creatingSession || sourceUnavailable}
              className="min-h-12 max-h-40 resize-none overflow-y-auto rounded-none border-0 bg-transparent p-0 text-sm shadow-none focus-visible:ring-0 dark:bg-transparent"
            />
          </div>
          {/* No wrapping: the option controls shrink instead, so the takeover
              switch and send button always stay on the same row. */}
          <div className="flex items-center gap-1 px-3 pb-3 pt-2">
            <AttachmentButton
              attachments={attachments}
              onAttach={add}
              isDragging={isDragging}
              className="size-8"
              allowedMimeTypes={allowedMimeTypes}
              disabled={!canUseAttachments || sourceUnavailable || creatingSession}
            />
            {hasSelectors ? (
              compactSelectors ? (
                <SelectionSettingsDrawer
                  requestOpenKey={selectorRequest}
                  onClose={() => setSelectorRequest(0)}
                  disabled={selectorsDisabled}
                  permissionDisabled={permissionSelectorDisabled}
                  modelDisabled={modelSelectorDisabled}
                  reasoningDisabled={effortSelectorDisabled}
                  buttonLabel={tNew("selectionSettings")}
                  title={tNew("selectionSettings")}
                  description={tNew("selectionSettingsDescription")}
                  permissionLabel={tNew("permissionMode")}
                  modelLabel={tNew("modelAndReasoning")}
                  reasoningLabel={tNew("reasoning")}
                  permissionItems={permissionItems}
                  selectedPermission={selectedPermissionMode}
                  onPermissionChange={choosePermission}
                  modelItems={modelItems}
                  selectedModel={selectedModel}
                  selectedReasoning={selectedReasoning}
                  onModelChange={chooseModel}
                />
              ) : (
                <>
                {permissionItems.length > 0 ? (
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-8 min-w-0 shrink gap-1.5 rounded-xl px-2.5 text-muted-foreground"
                        disabled={permissionSelectorDisabled}
                      >
                        <span className="size-1.5 shrink-0 rounded-full bg-primary" />
                        <span className="min-w-0 truncate text-foreground">{permissionLabel}</span>
                        <ChevronDown className="size-3.5 opacity-60" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="start" className="w-64">
                      {permissionItems.map((item) => (
                        <DropdownMenuItem
                          key={item.id}
                          disabled={!item.enabled}
                          className={cn(
                            "items-start gap-2 py-2.5",
                            selectedPermissionMode === item.id && "text-primary focus:text-primary",
                          )}
                          onSelect={() => choosePermission(item.id)}
                        >
                          <Check className={cn("mt-0.5 size-3.5", selectedPermissionMode === item.id ? "opacity-100" : "opacity-0")} />
                          <span className="min-w-0 flex-1">
                            <span className="block font-medium leading-none">{item.label}</span>
                            {(item.enabled ? item.description : item.disabledReason) ? (
                              <span className="mt-1 block whitespace-normal text-xs leading-snug text-muted-foreground">
                                {item.enabled ? item.description : item.disabledReason}
                              </span>
                            ) : null}
                          </span>
                        </DropdownMenuItem>
                      ))}
                    </DropdownMenuContent>
                  </DropdownMenu>
                ) : null}
                {modelItems.length > 0 ? (
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-8 min-w-0 shrink gap-1.5 rounded-xl px-2.5 text-muted-foreground"
                        disabled={modelSelectorDisabled}
                      >
                        {effortItems.length > 0 ? <span className="text-foreground">{effortLabel}</span> : null}
                        {effortItems.length > 0 ? <span className="text-muted-foreground/50">·</span> : null}
                        <span className="min-w-0 max-w-40 truncate text-foreground">{modelLabel}</span>
                        <ChevronDown className="size-3.5 opacity-60" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="start" className="w-56">
                      {modelItems.length > 0 ? (
                        modelItems.map((modelItem) => {
                          const modelEfforts = modelItem.reasoningItems
                          if (modelEfforts.length === 0) {
                            return (
                              <DropdownMenuItem
                                key={modelItem.id}
                                disabled={!modelItem.enabled}
                                className="gap-2"
                                onSelect={() => chooseModel(modelItem.id, "")}
                              >
                                <Check className={cn("size-3.5", selectedModel === modelItem.id ? "opacity-100" : "opacity-0")} />
                                <span className="min-w-0 flex-1">
                                  <span className="block truncate">{modelItem.label}</span>
                                  {!modelItem.enabled && modelItem.disabledReason ? (
                                    <span className="block truncate text-xs text-muted-foreground">
                                      {modelItem.disabledReason}
                                    </span>
                                  ) : null}
                                </span>
                              </DropdownMenuItem>
                            )
                          }
                          return (
                            <DropdownMenuSub key={modelItem.id}>
                              <DropdownMenuSubTrigger
                                className="gap-2"
                                disabled={effortSelectorDisabled || !modelItem.enabled}
                              >
                                <Check className={cn("size-3.5", selectedModel === modelItem.id ? "opacity-100" : "opacity-0")} />
                                <span className="max-w-40 truncate" title={modelItem.disabledReason ?? undefined}>
                                  {modelItem.label}
                                </span>
                              </DropdownMenuSubTrigger>
                              <DropdownMenuSubContent className="w-56">
                                {modelEfforts.map((item) => (
                                  <DropdownMenuItem
                                    key={item.id}
                                    disabled={!item.enabled}
                                    className="gap-2"
                                    onSelect={() => chooseModel(modelItem.id, item.id)}
                                  >
                                    <Check className={cn(
                                      "size-3.5",
                                      selectedModel === modelItem.id && selectedReasoning === item.id ? "opacity-100" : "opacity-0",
                                    )} />
                                    <span className="min-w-0 flex-1">
                                      <span className="block truncate">{item.label}</span>
                                      {!item.enabled && item.disabledReason ? (
                                        <span className="block truncate text-xs text-muted-foreground">
                                          {item.disabledReason}
                                        </span>
                                      ) : null}
                                    </span>
                                  </DropdownMenuItem>
                                ))}
                              </DropdownMenuSubContent>
                            </DropdownMenuSub>
                          )
                        })
                      ) : null}
                    </DropdownMenuContent>
                  </DropdownMenu>
                ) : null}
                </>
              )
            ) : null}
            <div
              role="switch"
              aria-checked={session.takeover}
              aria-disabled={!connectorOnline || takeoverBusy || creatingSession}
              tabIndex={connectorOnline && !takeoverBusy && !creatingSession ? 0 : -1}
              className={cn(
                "ml-auto flex h-8 shrink-0 items-center gap-2 rounded-xl px-2.5 text-sm text-muted-foreground transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
                connectorOnline && !takeoverBusy && !creatingSession && "cursor-pointer hover:bg-accent hover:text-accent-foreground",
                (!connectorOnline || takeoverBusy || creatingSession) && "opacity-50",
                session.takeover && "text-foreground",
              )}
              onClick={() => {
                if (!connectorOnline || takeoverBusy || creatingSession) return
                onToggleTakeover()
              }}
              onKeyDown={(event) => {
                if (!connectorOnline || takeoverBusy || creatingSession) return
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault()
                  onToggleTakeover()
                }
              }}
            >
              {takeoverBusy ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <Switch
                  size="sm"
                  checked={session.takeover}
                  tabIndex={-1}
                  aria-hidden
                  className="pointer-events-none"
                />
              )}
              {tSession("takeover")}
            </div>
            <span className="mx-1 h-5 w-px shrink-0 bg-border" />
            {showInterrupt ? <Button
              type="button"
              size="icon"
              variant="destructive"
              aria-label={tSession("interrupt")}
              className="size-8 rounded-full"
              disabled={interrupting}
              onClick={onInterrupt}
            >
              {interrupting ? <Loader2 className="size-4 animate-spin" /> : <Square className="size-4" />}
            </Button> : null}
            <Button
              type="button"
              size="icon"
              aria-label={isRunning && !slashIntent && canUseSteer ? tSession("sendWhileRunning") : tSession("send")}
              className="size-8 rounded-full"
              disabled={!(canSubmitCommand || canSubmitMessage || canSubmitSteer)}
              onClick={() => void submit()}
            >
              {sending || messagePending || steerPending ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <ArrowUp className="size-4" />
              )}
            </Button>
          </div>
        </div>
      </div>
    </div>
  )
}

function commandMatchesQuery(command: RuntimeCommand, query: string | null): boolean {
  if (query === null) return false
  const normalized = query.toLowerCase()
  if (!normalized) return true
  return (
    fuzzyIncludes(command.id.toLowerCase(), normalized) ||
    fuzzyIncludes(command.title.toLowerCase(), normalized) ||
    command.aliases.some((alias) => fuzzyIncludes(alias.toLowerCase(), normalized))
  )
}

function fuzzyIncludes(value: string, query: string): boolean {
  if (value.includes(query)) return true
  let index = 0
  for (const char of value) {
    if (char === query[index]) index += 1
    if (index === query.length) return true
  }
  return query.length === 0
}

function effectiveRuntimeStatus(
  runtimeState: SessionRuntimeState | null | undefined,
  session: SessionView,
): RuntimeStatusValue {
  if (runtimeState) return runtimeState.status
  return session.connectorStatus === "offline" ? "disconnected" : session.status
}
