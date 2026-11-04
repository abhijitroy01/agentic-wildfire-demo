/// <reference types="vite/client" />

/**
 * Vite Environment Variable Type Declarations
 * @see https://vitejs.dev/guide/env-and-mode.html#intellisense-for-typescript
 */
interface ImportMetaEnv {
  // Build flags. Published builds set VITE_EVACUATION_ENABLED to "true" (deployment/publish_workspace.py).
  readonly VITE_EVACUATION_ENABLED?: string;
  readonly VITE_DEBUG?: string;

  // Data Service URLs
  readonly VITE_POPULATION_SERVICE_URL?: string;
  readonly VITE_VEGETATION_SERVICE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
