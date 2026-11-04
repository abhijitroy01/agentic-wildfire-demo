/**
 * Response Registry Context Provider
 *
 * Provides the known fires and stage 2's selected scenario
 * to all components without prop drilling.
 *
 * Usage:
 * 1. Wrap your app with <ResponseRegistryProvider>
 * 2. Access registry via useResponseRegistryContext() hook
 *
 * See architecture/10-ui-architecture.md for full documentation.
 */

import type React from "react";
import { createContext,useContext,type ReactNode } from "react";
import { useResponseRegistry,type UseResponseRegistryReturn } from "./hooks/useResponseRegistry";

/** The context carries exactly what useResponseRegistry returns, declared once in the hook. */
export type ResponseRegistryContextValue = UseResponseRegistryReturn;

// --- Context ---

const ResponseRegistryContext =
  createContext<ResponseRegistryContextValue | null>(null);

// --- Provider Component ---

interface ResponseRegistryProviderProps {
  children: ReactNode;
}

/**
 * Provider component that wraps the app and provides registry access
 *
 * @example
 * // In index.tsx or App.tsx
 * <ResponseRegistryProvider>
 *   <App />
 * </ResponseRegistryProvider>
 */
export const ResponseRegistryProvider: React.FC<
  ResponseRegistryProviderProps
> = ({ children }) => {
  const registry = useResponseRegistry();

  return (
    <ResponseRegistryContext.Provider value={registry}>
      {children}
    </ResponseRegistryContext.Provider>
  );
};

// --- Consumer Hook ---

/**
 * Hook to access the Response Registry context
 *
 * @throws Error if used outside of ResponseRegistryProvider
 *
 * @example
 * const { registerFires, getKnownFires } = useResponseRegistryContext();
 *
 * // Register fires from agent response
 * registerFires([{ fireId: '123', fireName: 'Example Fire', ... }]);
 *
 * // Get fires for dropdown
 * const fires = getKnownFires();
 */
export const useResponseRegistryContext = (): ResponseRegistryContextValue => {
  const context = useContext(ResponseRegistryContext);

  if (!context) {
    throw new Error(
      "useResponseRegistryContext must be used within a ResponseRegistryProvider. " +
        "Make sure your component tree is wrapped with <ResponseRegistryProvider>."
    );
  }

  return context;
};

// --- Exports ---

export default ResponseRegistryContext;
