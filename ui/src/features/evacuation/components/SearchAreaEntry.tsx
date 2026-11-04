import type React from "react";
import { useId } from "react";
import { REPORTED_PLACE_MIN_LENGTH } from "../services/api";

/**
 * The hand-off from 1 · Investigate: the selected answer's place, which 2 · Evacuation & Crew Access sends as a
 * reported place, so the server geocodes its representative U.S. point under the shared daily limits. The place is
 * third-party text from agent tools and renders as plain text.
 */
export default function SearchAreaEntry({ place, onSimulate }: {
  place: string; onSimulate: (place: string) => void;
}): React.ReactElement {
  const hint = useId();
  const tooShort = place.trim().length < REPORTED_PLACE_MIN_LENGTH;
  return <section className="workspace-card search-area" aria-label="Search area">
    <h3>Search area: {place}</h3>
    <p>2 · Evacuation &amp; Crew Access resolves this U.S. place on the server and prepares a new scenario there, with no
      specific fire selected.</p>
    <button type="button" disabled={tooShort} aria-describedby={tooShort ? hint : undefined}
      onClick={() => onSimulate(place)}>Simulate this search area</button>
    {tooShort && <p id={hint}>Enter a fuller place name in 2 · Evacuation &amp; Crew Access.</p>}
  </section>;
}
