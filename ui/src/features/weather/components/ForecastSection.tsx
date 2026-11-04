import MetadataDisplay from "@/features/chat/components/MetadataDisplay";
import type { components } from "@/schemas/generated/api-types";
import Accordion from "@/shared/components/Accordion";
import { observationValue } from "@/shared/domain/incidentObservations";
import { useState } from "react";
import "./ForecastSection.css";

/** Forecast presentation extends generated wire types only for retained provider display fields. */
export type ForecastPeriod = components["schemas"]["ForecastPeriod"] & {hour?: string; weatherDescription?: string};
export type ForecastDay = Omit<components["schemas"]["ForecastDay"], "periods"> & {periods?: ForecastPeriod[]};
export type ForecastData = Omit<components["schemas"]["MultiDayForecast"], "days"> & {
  days?: ForecastDay[]; textSummary?: string; hasRedFlagWarning?: boolean; hasHighWindWarning?: boolean;
};

/** Regroup source instants into the same timezone used by their visible dates and times. */
export function forecastDays(data: ForecastData, timeZone: string): ForecastDay[] {
  const grouped = new Map<string, ForecastDay>();
  const formatter = new Intl.DateTimeFormat("en-CA", {timeZone, year:"numeric", month:"2-digit", day:"2-digit"});
  const periods = (data.days || []).flatMap(day => day.periods || [])
    .filter(period => period.timestamp && Number.isFinite(Date.parse(period.timestamp)))
    .sort((a, b) => Date.parse(a.timestamp!)-Date.parse(b.timestamp!));
  for (const period of periods) {
    const parts = formatter.formatToParts(new Date(period.timestamp!));
    const value = (type: string): string => parts.find(part => part.type === type)!.value;
    const date = `${value("year")}-${value("month")}-${value("day")}`;
    if (!grouped.has(date)) grouped.set(date, {date, dayName:new Date(period.timestamp!).toLocaleDateString("en-US", {
      weekday:"long", timeZone}), periods:[]});
    grouped.get(date)!.periods!.push(period);
  }
  const days = [...grouped.values()];
  // Undated provider entries remain visible, with no invented time or local-day association.
  for (const day of data.days || []) {
    const undated = (day.periods || []).filter(period => !period.timestamp || !Number.isFinite(Date.parse(period.timestamp)));
    if (undated.length || !day.periods?.length) days.push({...day, periods:undated});
  }
  return days;
}

export default function ForecastSection({ forecastData, metadata, usesAccordion, timeZone = "UTC" }: {
  forecastData: ForecastData; metadata?: Record<string, string | number | null | undefined> | null;
  usesAccordion?: boolean; timeZone?: string;
}): React.JSX.Element {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const days = forecastDays(forecastData, timeZone);
  return <div className="forecast-section">
    {usesAccordion && <h3>Weather forecast</h3>}
    {metadata && <MetadataDisplay metadata={metadata} variant={usesAccordion ? "compact" : "default"} />}
    <small>Dates and times: {timeZone}</small>
    {days.map((day, index) => {
      const key = `${day.date}-${index}`;
      const temperatures = (day.periods || []).flatMap(period => typeof period.temperature?.value === "number" &&
        Number.isFinite(period.temperature.value) ? [{value:period.temperature.value, unit:period.temperature.unit}] : []);
      const low = temperatures.length ? temperatures.reduce((a, b) => a.value < b.value ? a : b) : day.summary?.lowTemp;
      const high = temperatures.length ? temperatures.reduce((a, b) => a.value > b.value ? a : b) : day.summary?.highTemp;
      const isExpanded = expanded[key] ?? index === 0;
      return <Accordion key={key} title={`${day.dayName || "Forecast"} · ${day.date || "Date unavailable"}`}
        isExpanded={isExpanded} onToggle={() => setExpanded(previous => ({...previous, [key]:!isExpanded}))} variant="forecast"
        headerContent={low?.value != null && high?.value != null ? <span className="temp-range-badge">{observationValue(low)}–{observationValue(high)}</span> : undefined}>
        <div className="forecast-periods-grid">{(day.periods || []).map((period, i) =>
          <article key={period.timestamp || i} className="period-card-compact">
            <div className="period-time-compact">{period.timestamp && Number.isFinite(Date.parse(period.timestamp))
              ? new Date(period.timestamp).toLocaleTimeString("en-US", {hour:"numeric", minute:"2-digit", timeZone})
              : "Time unavailable"}</div>
            <dl className="period-metrics-compact">{[["Temperature", period.temperature], ["Wind", period.wind?.speed],
              ["Humidity", period.relativeHumidity], ["Rain probability", period.precipitation?.probability]]
              .map(([name, value]) => <div key={String(name)} className="metric-compact"><dt>{String(name)}</dt>
                <dd>{observationValue(value)}</dd></div>)}</dl>
            {period.weatherDescription && <p className="weather-desc-compact">{period.weatherDescription}</p>}
          </article>)}</div>
        {!day.periods?.length && <p>No timestamped forecast periods supplied.</p>}
      </Accordion>;
    })}
  </div>;
}
