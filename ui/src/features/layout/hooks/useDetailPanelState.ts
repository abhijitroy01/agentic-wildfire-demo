import type { ParsedResponse } from "@/shared/services/agentResponseParser";
import { useCallback, useState } from "react";

/** Evidence belongs to an answer, including its source location and timestamp. */
interface DetailPanelState {
  evidence: ParsedResponse | null;
  receiveEvidence: (answer: ParsedResponse) => void;
  inspectEvidence: (answer: ParsedResponse) => void;
  isDetailPanelOpen: boolean;
  setIsDetailPanelOpen: (open: boolean) => void;
}
export function useDetailPanelState(): DetailPanelState {
  const [evidence, setEvidence] = useState<ParsedResponse | null>(null);
  const [isDetailPanelOpen, setIsDetailPanelOpen] = useState(false);
  const receiveEvidence = useCallback((answer: ParsedResponse) => {
    setEvidence(answer);
    if (answer.audioData?.languages) setIsDetailPanelOpen(true);
  }, []);
  const inspectEvidence = useCallback((answer: ParsedResponse) => {
    setEvidence(answer);
    setIsDetailPanelOpen(true);
  }, []);
  return { evidence, receiveEvidence, inspectEvidence, isDetailPanelOpen, setIsDetailPanelOpen };
}
