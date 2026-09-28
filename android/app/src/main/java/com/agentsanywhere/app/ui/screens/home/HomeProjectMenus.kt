package com.agentsanywhere.app.ui.screens.home

import androidx.compose.foundation.lazy.items
import androidx.compose.runtime.Composable
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.res.stringResource
import com.agentsanywhere.app.R
import com.agentsanywhere.app.feature.sessions.ProjectDeviceAgentFilter
import com.agentsanywhere.app.feature.sessions.ProjectSessionStatusFilter
import com.agentsanywhere.app.model.AgentDevice
import com.agentsanywhere.app.model.runtimeTypeLabel
import com.agentsanywhere.app.ui.designsystem.AAAnchoredDropdownMenu
import com.agentsanywhere.app.ui.designsystem.AAAnchoredPopup
import com.agentsanywhere.app.ui.designsystem.AADropdownMenuItem
import com.agentsanywhere.app.ui.designsystem.AADropdownMenuLabel

@Composable
internal fun HomeProjectAnchoredPopup(
    anchorBounds: Rect,
    onDismiss: () -> Unit,
    content: @Composable () -> Unit,
) {
    AAAnchoredPopup(anchorBounds = anchorBounds, onDismissRequest = onDismiss, content = content)
}

@Composable
internal fun HomeProjectFilterMenu(
    anchorBounds: Rect,
    selected: ProjectSessionStatusFilter,
    deviceAgentFilter: ProjectDeviceAgentFilter,
    devices: List<AgentDevice>,
    agentRuntimes: List<String>,
    onDismiss: () -> Unit,
    onSelectStatus: (ProjectSessionStatusFilter) -> Unit,
    onSelectDevice: (String?) -> Unit,
    onSelectAgent: (String?) -> Unit,
    onClearDeviceAgentFilter: () -> Unit,
) {
    AAAnchoredDropdownMenu(anchorBounds = anchorBounds, onDismissRequest = onDismiss) {
        item("status-label") { AADropdownMenuLabel(stringResource(R.string.home_project_session_status)) }
        items(ProjectSessionStatusFilter.entries, key = { "status-${it.name}" }) { status ->
            val label = when (status) {
                ProjectSessionStatusFilter.Active -> R.string.home_project_filter_active
                ProjectSessionStatusFilter.Archived -> R.string.home_project_filter_archived
                ProjectSessionStatusFilter.All -> R.string.home_project_filter_all
            }
            AADropdownMenuItem(
                text = stringResource(label),
                selected = selected == status,
                onClick = { onSelectStatus(status); onDismiss() },
            )
        }
        item("devices-label") { AADropdownMenuLabel(stringResource(R.string.home_project_filter_devices)) }
        item("all-devices") {
            AADropdownMenuItem(
                text = stringResource(R.string.home_project_filter_all_devices),
                selected = deviceAgentFilter.connectorId == null,
                onClick = { onSelectDevice(null); onDismiss() },
            )
        }
        items(devices, key = { "device-${it.id}" }) { device ->
            AADropdownMenuItem(
                text = device.name.trim().ifBlank { device.id },
                selected = deviceAgentFilter.connectorId == device.id,
                onClick = { onSelectDevice(device.id); onDismiss() },
            )
        }
        item("agents-label") { AADropdownMenuLabel(stringResource(R.string.home_project_filter_agents)) }
        item("all-agents") {
            AADropdownMenuItem(
                text = stringResource(R.string.home_project_filter_all_agents),
                selected = deviceAgentFilter.runtime == null,
                onClick = { onSelectAgent(null); onDismiss() },
            )
        }
        items(agentRuntimes, key = { "agent-$it" }) { runtime ->
            AADropdownMenuItem(
                text = runtime.runtimeTypeLabel(),
                selected = deviceAgentFilter.runtime == runtime,
                onClick = { onSelectAgent(runtime); onDismiss() },
            )
        }
        if (deviceAgentFilter.active) item("clear-device-agent") {
            AADropdownMenuItem(
                text = stringResource(R.string.home_project_filter_clear),
                selected = false,
                onClick = { onClearDeviceAgentFilter(); onDismiss() },
            )
        }
    }
}
