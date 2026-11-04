import CameraFeedModal from "@/features/camera/components/CameraFeedModal";
import ChatPanel from "@/features/chat/components/ChatPanel";
import { EvacuationWorkspace, ScenarioStrip, SearchAreaEntry, type SearchAreaRequest } from "@/features/evacuation";
import ResponseDashboardPanel from "@/features/reports/components/ResponseDashboardPanel";
import StandaloneExerciseEntry from "@/features/reports/components/StandaloneExerciseEntry";
import { SIMULATION_ENABLED } from "@/shared/config";
import type React from "react";
import { useCallback,useState } from "react";
import "./App.css";
import AppHeader from "./AppHeader";
import AgentActivity from "@/shared/components/AgentActivity";
import WorkspaceHeading from "@/shared/components/WorkspaceHeading";
import "./workspace-theme.css";
import WorkspaceMap from "./WorkspaceMap";
import DetailPanel from "./details/DetailPanel";
import DetailPanelButton from "./details/DetailPanelButton";

// Layout hooks — each owns a focused slice of App state
import { useAgentSubmission } from "../hooks/useAgentSubmission";
import { useCameraModal } from "../hooks/useCameraModal";
import { useDetailPanelState } from "../hooks/useDetailPanelState";
import { useViewMode } from "../hooks/useViewMode";

/** Application shell with one simulation workspace. */
function App(): React.ReactElement {
  // --- View: the numbered workflow stage ---
  const { viewMode, selectView, exerciseOpen, openExercise, closeExercise } = useViewMode();
  // Stages 2 and 3 share one kept-mounted workspace; the standalone exercise temporarily replaces stage 3.
  const workspaceVisible = viewMode === "evacuation" || (viewMode === "response" && !exerciseOpen);

  // --- Camera modal ---
  const { isCameraModalOpen, cameraFireName, closeCameraModal } = useCameraModal();

  // --- Panel data & visibility ---
  const panelState = useDetailPanelState();

  // --- Agent submission, chat, map ---
  const {
    messages,
    isLoading,
    inputText,
    currentLoadingMessage,
    setInputText,
    mapData,
    centerLocation,
    handleSubmit,
    toolEvents,
    supervisorRun,
    selectAnswer,
    searchArea,
    canResume,
    resumeAccepted,
  } = useAgentSubmission({
    onEvidence: panelState.receiveEvidence,
  });

  // --- Stage 1 → 2 hand-off: one request per "Simulate this search area" click, started once by stage 2 ---
  const [searchAreaRequest, setSearchAreaRequest] = useState<SearchAreaRequest | null>(null);
  const simulateSearchArea = (place: string): void => {
    setSearchAreaRequest({id: crypto.randomUUID(), kind:"search-area", place});
    selectView("evacuation");
  };
  const simulateReportedFire = (assessmentId: string): void => {
    setSearchAreaRequest({id: crypto.randomUUID(), kind:"reported-fire", assessmentId});
    selectView("evacuation");
  };
  const handleSearchAreaHandled = useCallback((): void => setSearchAreaRequest(null), []);

  // --- Render ---
  return (
    <div className="App">
      <AppHeader viewMode={viewMode} onNavigate={selectView} />
      <ScenarioStrip />

      {SIMULATION_ENABLED ? <div hidden={!workspaceVisible} style={{width: "100%", minWidth: 0, overflow: "auto", flex: workspaceVisible ? 1 : undefined}}>
        <EvacuationWorkspace visible={workspaceVisible} stage={viewMode === "response" ? "response" : "evacuation"}
          exerciseOpen={exerciseOpen} onOpenExercise={openExercise}
          searchAreaRequest={searchAreaRequest} onSearchAreaHandled={handleSearchAreaHandled} /></div>
        : viewMode === "response" && <main className="workspace-surface" aria-label="Response Tracker" hidden={exerciseOpen}>
          <WorkspaceHeading eyebrow="SELECTED RUN REPLAY" title="Response Tracker"
            description="This build has no evacuation simulations, so there is no selected run to replay." />
          <StandaloneExerciseEntry exerciseOpen={exerciseOpen} onOpen={openExercise} />
        </main>}
      {viewMode === "chat" && (
        <main className="workspace-surface investigation-workspace">
          <WorkspaceHeading eyebrow="AGENT ASSISTANT" title="Investigation"
            description="Ask about local fires, weather and response guidance. Follow the agents and inspect their sources." />
          <div className="main-content investigation-columns">
            <div className="investigation-assistant">
            <ChatPanel
              messages={messages}
              onInspectEvidence={answer => {
                selectAnswer(answer);
                panelState.inspectEvidence(answer);
              }}
              isLoading={isLoading}
              submissionBlocked={canResume}
              currentLoadingMessage={currentLoadingMessage}
              inputText={inputText}
              onInputChange={setInputText}
              onSubmit={handleSubmit}
            />
              {canResume && <div role="status">
                <p>The previous investigation is retained. Check its outcome before sending another request.</p>
                <button type="button" disabled={isLoading} onClick={resumeAccepted}>Check accepted investigation</button>
              </div>}
              <AgentActivity events={toolEvents} run={supervisorRun} />
              {SIMULATION_ENABLED && searchArea && <SearchAreaEntry place={searchArea} onSimulate={simulateSearchArea} />}
            </div>

            <WorkspaceMap mapData={mapData} centerLocation={centerLocation}
              onSimulateReportedFire={SIMULATION_ENABLED ? simulateReportedFire : undefined} />
          </div>

          <DetailPanelButton
            citationCount={panelState.evidence?.knowledgeBaseCitations?.length || 0}
            audioCount={panelState.evidence?.audioData?.summary?.totalLanguages || 0}
            forecastDays={panelState.evidence?.forecastData?.days?.length || 0}
            weatherCount={panelState.evidence?.locations.filter(item => item.currentWeather).length || 0}
            onClick={() => panelState.setIsDetailPanelOpen(true)}
            isOpen={panelState.isDetailPanelOpen}
          />
          <DetailPanel
            citations={panelState.evidence?.knowledgeBaseCitations}
            audioData={panelState.evidence?.audioData}
            forecastData={panelState.evidence?.forecastData}
            locations={panelState.evidence?.locations}
            audioMetadata={{ location: panelState.evidence?.location?.name || "Discussed incident" }}
            forecastMetadata={{ location: panelState.evidence?.location?.name || "Discussed location",
              source: panelState.evidence?.forecastData?.provider || "DTN" }}
            isOpen={panelState.isDetailPanelOpen}
            onClose={() => panelState.setIsDetailPanelOpen(false)}
          />
        </main>
      )}

          <ResponseDashboardPanel
            isOpen={viewMode === "response" && exerciseOpen}
            inResponseTracker={viewMode === "response"}
            onClose={closeExercise}
            renderMap={(center, trucks) => <WorkspaceMap mapData={mapData} centerLocation={center || centerLocation}
              trucks={trucks} label="response" />}
          />

      {isCameraModalOpen && <CameraFeedModal
        isOpen={isCameraModalOpen}
        onClose={closeCameraModal}
        fireName={cameraFireName}
      />}
    </div>
  );
}

export default App;
