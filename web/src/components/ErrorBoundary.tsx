import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/**
 * Keeps one failed panel from blanking the whole dashboard.
 *
 * React has no hook equivalent: a component that throws during render can only
 * be caught by a class with `getDerivedStateFromError`. The file pickers live
 * outside this boundary on purpose, so a broken run can be replaced without a
 * reload.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Meridian dashboard render error", error, info.componentStack);
  }

  private retry = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    if (this.state.error) {
      return (
        <section role="alert" className="bad" data-testid="render-error">
          <h2>This run could not be displayed</h2>
          <p>{this.state.error.message}</p>
          <p>
            The file pickers above still work: load a different run, or try this one again.
          </p>
          <button type="button" onClick={this.retry}>
            Try again
          </button>
        </section>
      );
    }
    return this.props.children;
  }
}
