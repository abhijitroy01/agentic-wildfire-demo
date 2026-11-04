import type { components } from "@/schemas/generated/api-types";
import type {
  AssessmentResult, FireRiskJobReference, FireRiskPreparationError,
} from "@/features/fire-exposure/generated/contracts";
import { answerAssessments, answerJobs, preparationError } from "@/features/fire-exposure/services/assessment";

export type LocationData = components["schemas"]["FireLocation"] & { name?: string };

type UniversalResponse = components["schemas"]["UniversalResponse"];
type UniversalContent = components["schemas"]["UniversalContent"];

export interface ParsedLocation {
  latitude?: number;
  longitude?: number;
  lat?: number;
  lon?: number;
  name?: string;
  [key: string]: unknown;
}
export type ParsedAudioData = components["schemas"]["AudioData"];
export type ParsedForecastData = components["schemas"]["MultiDayForecast"];

export interface ParsedResponse {
  location: ParsedLocation | null;
  response: string;
  locations: LocationData[];
  fireExposureData: AssessmentResult[];
  fireExposureJobs?: FireRiskJobReference[];
  fireExposureError?: FireRiskPreparationError;
  audioData: ParsedAudioData | null;
  forecastData: ParsedForecastData | null;
  knowledgeBaseCitations?: KnowledgeBaseCitation[] | null;
}

export interface KnowledgeBaseCitation {
  documentName?: string;
  sourceUrl?: string;
  publisher?: string;
  sourceDate?: string;
  retrievedAt?: string;
  excerpt?: string;
}

export const parseAgentCoreResponse = async (
  agentCoreResponse: ReadableStream | string | unknown,
): Promise<ParsedResponse> => {
  const responseText = await extractResponseText(agentCoreResponse);
  const structuredResponse = extractStructuredResponse(responseText);
  return transformToUIFormat(structuredResponse.content);
};

async function extractResponseText(response: unknown): Promise<string> {
  if (typeof response === "string") {
    return response;
  }

  if (response instanceof ReadableStream) {
    const reader = response.getReader();
    const decoder = new TextDecoder();
    let result = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      result += decoder.decode(value, { stream: true });
    }
    return result;
  }

  return JSON.stringify(response);
}

/**
 * Extracts a UniversalResponse from a JSON string.
 *
 * AG-UI delivers the snapshot as a fully-validated `UniversalResponse` object
 * already, so this is just a JSON.parse with type narrowing. Any wrapping
 * envelopes from older runtime protocols are no longer produced and have
 * been removed.
 */
function extractStructuredResponse(responseText: string): UniversalResponse {
  const parsed = JSON.parse(responseText);
  return parsed as UniversalResponse;
}

function transformToUIFormat(content: UniversalContent): ParsedResponse {
  const result: ParsedResponse = {
    location: content.location || null,
    response: content.response || "",
    locations: content.mapData?.locations || [],
    fireExposureData: [],
    audioData: content.audioData || null,
    forecastData: content.forecastData || null,
    knowledgeBaseCitations: content.knowledgeBaseCitations || null,
  };

  result.fireExposureData = answerAssessments(content.fireExposureData, result.locations);
  result.fireExposureJobs = answerJobs(content.fireExposureJobs);
  result.fireExposureError = preparationError(content.fireExposureError);

  return result;
}

const AgentResponseParser = {
  parse: parseAgentCoreResponse,
};

export default AgentResponseParser;
