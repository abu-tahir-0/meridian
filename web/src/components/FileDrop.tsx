import { useRef, useState } from "react";
import { parseRun, type RunResult } from "../schema";

interface Props {
  label: string;
  onLoad: (run: RunResult, name: string) => void;
}

/** Load a run result from disk by picking or dropping a JSON file. */
export function FileDrop({ label, onLoad }: Props) {
  const input = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);
  // dragenter/leave fire for every child the pointer crosses, so a plain
  // boolean goes false while the file is still over the box. Count the nesting.
  const dragDepth = useRef(0);
  const [dragging, setDragging] = useState(false);

  async function handle(file: File | undefined) {
    if (!file) return;
    const parsed = parseRun(await file.text());
    if (parsed.ok) {
      setError(null);
      onLoad(parsed.run, file.name);
    } else {
      setError(parsed.error);
    }
  }

  return (
    <div
      className={dragging ? "drop dragging" : "drop"}
      onDragEnter={(e) => {
        e.preventDefault();
        dragDepth.current += 1;
        setDragging(true);
      }}
      onDragLeave={() => {
        dragDepth.current = Math.max(0, dragDepth.current - 1);
        if (dragDepth.current === 0) setDragging(false);
      }}
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        dragDepth.current = 0;
        setDragging(false);
        void handle(e.dataTransfer.files[0]);
      }}
    >
      <button type="button" onClick={() => input.current?.click()}>
        {label}
      </button>
      <input
        ref={input}
        type="file"
        accept="application/json,.json"
        aria-label={label}
        hidden
        onChange={(e) => void handle(e.target.files?.[0])}
      />
      <span className="muted"> or drop a result.json here</span>
      {error && (
        <p role="alert" className="bad">
          {error}
        </p>
      )}
    </div>
  );
}
