import { incidentObservations, observationDetails } from "@/shared/domain/incidentObservations";
import type { LocationData } from "@/shared/services/agentResponseParser";

/** Uses the same source projection as the incident cards and map. */
export default function CurrentWeatherSection({ locations }: { locations: LocationData[] }): React.JSX.Element | null {
  const observations = incidentObservations({content: {mapData: {locations}}}).filter(item => item.weather);
  if (!observations.length) return null;
  return <section aria-label="Weather observations" className="current-weather-section">
    <h3>Weather observations</h3>
    {observations.map(item => <article key={item.id}>
      <h4>{item.name}</h4>
      <p>{item.latitude.toFixed(4)}, {item.longitude.toFixed(4)} · DTN</p>
      <dl>{observationDetails({...item, fire: undefined}).map(([label, value]) =>
        <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
      <details><summary>Raw provider observation</summary><pre>{JSON.stringify(item.weather, null, 2)}</pre></details>
    </article>)}
  </section>;
}
