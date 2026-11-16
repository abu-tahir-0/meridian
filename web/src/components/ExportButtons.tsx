import { useState } from "react";
import { downloadCsv } from "../export";

interface Props {
  /** What is being exported, for the button names and status message. */
  label: string;
  filename: string;
  markdown: string;
  csv: string;
}

/** Copy and download controls for one table. */
export function ExportButtons({ label, filename, markdown, csv }: Props) {
  const [message, setMessage] = useState("");

  async function copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(markdown);
      setMessage(`${label} copied as Markdown.`);
    } catch {
      setMessage("Could not copy: the browser blocked clipboard access.");
    }
  }

  function download(): void {
    downloadCsv(filename, csv);
    setMessage(`Downloaded ${filename}.`);
  }

  return (
    <div className="toolbar">
      <button type="button" onClick={() => void copy()}>
        {`Copy ${label} as Markdown`}
      </button>
      <button type="button" onClick={download}>
        {`Download ${label} CSV`}
      </button>
      {message && (
        <span role="status" className="muted">
          {message}
        </span>
      )}
    </div>
  );
}
