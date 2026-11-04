import { useCallback,useEffect,useRef,useState } from "react";
import { fromUSInput } from "./units";

export function UnitInput({ label, value, factor, min, max, onChange }: {
  label: string; value: number | ""; factor: number; min: number; max: number;
  onChange: (value: number | "") => void;
}) : React.JSX.Element {
  const display = useCallback((v: number | ""): string => v === "" ? "" : String(Number((v * factor).toFixed(2))), [factor]);
  const [draft, setDraft] = useState(() => display(value));
  const sent = useRef(value);
  useEffect(() => {
    if (value !== sent.current) setDraft(display(value));
    sent.current = value;
  }, [value, display]);
  return <label>{label}<input type="number" step="any"
    min={Number((min * factor).toFixed(2))} max={Number((max * factor).toFixed(2))}
    value={draft} onChange={e => {
      setDraft(e.target.value);
      sent.current = fromUSInput(e.target.value, factor, min, max);
      onChange(sent.current);
    }} onBlur={() => setDraft(display(value))}/></label>;
}
