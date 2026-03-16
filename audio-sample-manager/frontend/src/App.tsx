import { BrowserRouter, Routes, Route } from "react-router-dom";
import { Navbar } from "./components/Navbar";
import { BrowsePage } from "./pages/BrowsePage";
import { SamplePage } from "./pages/SamplePage";
import { LoginPage } from "./pages/LoginPage";
import { RegisterPage } from "./pages/RegisterPage";
import { CollectionsPage } from "./pages/CollectionsPage";

export default function App() {
  return (
    <BrowserRouter>
      <Navbar />
      <main>
        <Routes>
          <Route path="/" element={<BrowsePage />} />
          <Route path="/samples/:id" element={<SamplePage />} />
          <Route path="/login" element={<LoginPage />} />
          <Route path="/register" element={<RegisterPage />} />
          <Route path="/collections" element={<CollectionsPage />} />
        </Routes>
      </main>
    </BrowserRouter>
  );
}
