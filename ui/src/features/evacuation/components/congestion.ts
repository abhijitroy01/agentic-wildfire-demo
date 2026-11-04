import type { Policy } from "../artifacts";

export type Congestion = {
  slowMinutes: number; averageSlow: number; peak: number; peakTime: number; endSlow: number;
};

export function congestion(policy: Policy, horizon: number): Congestion | null {
  const series = policy?.series;
  if (!Number.isFinite(horizon) || horizon <= 0 || !Array.isArray(series) || series.length < 2
    || series[0]?.t !== 0 || series.at(-1)?.t !== horizon) return null;
  let area = 0, peak = 0, peakTime = 0;
  for (let i = 0; i < series.length; i++) {
    const sample = series[i];
    if (!sample || !Number.isFinite(sample.t) || !Number.isInteger(sample.queue) || sample.queue < 0
      || (i > 0 && sample.t <= series[i - 1].t)) return null;
    if (sample.queue > peak) { peak = sample.queue; peakTime = sample.t; }
    if (i) area += (sample.queue + series[i - 1].queue) / 2 * (sample.t - series[i - 1].t);
  }
  return { slowMinutes: area / 60, averageSlow: area / horizon, peak, peakTime, endSlow: series.at(-1)!.queue };
}

export function comparison(policy: Policy, baseline: Policy, horizon: number) : { current: Congestion; reference: Congestion; slowDelta: number; exitDelta: number; waitingDelta: number; title: string; percent: number | null; } | null {
  const current = congestion(policy, horizon), reference = baseline && congestion(baseline, horizon);
  const valid = [policy, baseline].every(p => p?.summary?.valid_engine_run === true && p?.summary?.valid_accounting === true
    && Number.isInteger(p.summary.exited_vehicles) && p.summary.exited_vehicles >= 0
    && Number.isInteger(p.summary.not_departed_vehicles) && p.summary.not_departed_vehicles >= 0);
  if (!valid || !current || !reference) return null;
  const slowDelta = Math.round((current.slowMinutes - reference.slowMinutes) * 10) / 10;
  const exitDelta = policy.summary.exited_vehicles - baseline.summary.exited_vehicles;
  const waitingDelta = policy.summary.not_departed_vehicles - baseline.summary.not_departed_vehicles;
  const title = slowDelta < 0
    ? exitDelta > 0 ? "Less time in slow traffic and more exits" : exitDelta < 0
      ? "Less time in slow traffic, but fewer exits" : "Less time in slow traffic; same exits"
    : slowDelta > 0
      ? exitDelta > 0 ? "More exits, but more time in slow traffic" : exitDelta < 0
        ? "More time in slow traffic and fewer exits" : "More time in slow traffic; same exits"
      : exitDelta > 0 ? "More exits; similar time in slow traffic" : exitDelta < 0
        ? "Fewer exits; similar time in slow traffic" : "Same exits and similar time in slow traffic";
  return { current, reference, slowDelta, exitDelta, waitingDelta, title,
    percent: reference.slowMinutes > 0 ? Math.abs(slowDelta) / reference.slowMinutes * 100 : null };
}
