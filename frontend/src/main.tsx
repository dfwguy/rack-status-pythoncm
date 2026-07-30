import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// Vendored Kaizen UI Foundations CSS (see styles/kaizen/README.md). Must load
// before our own styles so index.css/kaizen-oss.css can override it.
import "./styles/kaizen/base.css";
import "./styles/kaizen/components.css";
import "./index.css";
import "./kaizen-oss.css";
import App from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
