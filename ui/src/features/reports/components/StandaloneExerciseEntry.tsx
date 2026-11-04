import type React from "react";
import { useEffect,useRef } from "react";

/**
 * The secondary entry to the standalone firetruck exercise from 3 · Response Tracker. Closing the exercise returns
 * focus to this button. Keep it mounted (hide it instead) while the exercise is open, so it sees the close.
 */
export default function StandaloneExerciseEntry({ exerciseOpen, onOpen }: {
  exerciseOpen: boolean; onOpen: () => void;
}): React.ReactElement {
  const button = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(exerciseOpen);
  useEffect(() => {
    if (wasOpen.current && !exerciseOpen) button.current?.focus();
    wasOpen.current = exerciseOpen;
  }, [exerciseOpen]);
  return <section className="standalone-exercise" aria-label="Standalone exercise">
    <button ref={button} type="button" className="secondary" onClick={onOpen}>Standalone firetruck exercise</button>
    <p>A separate synthetic exercise; it is not linked to this run.</p>
  </section>;
}
