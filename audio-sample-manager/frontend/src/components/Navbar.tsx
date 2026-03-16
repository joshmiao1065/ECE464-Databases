import { Link, useNavigate } from "react-router-dom";
import { useAuthStore } from "../store/authStore";

export function Navbar() {
  const { username, logout } = useAuthStore();
  const navigate = useNavigate();

  const handleLogout = () => {
    logout();
    navigate("/");
  };

  return (
    <nav className="navbar">
      <Link to="/" className="nav-brand">
        SampleLib
      </Link>
      <div className="nav-links">
        <Link to="/">Browse</Link>
        {username && <Link to="/collections">Collections</Link>}
        {username ? (
          <>
            <span className="nav-username">{username}</span>
            <button onClick={handleLogout} className="nav-btn">Log out</button>
          </>
        ) : (
          <>
            <Link to="/login">Log in</Link>
            <Link to="/register" className="nav-btn">Sign up</Link>
          </>
        )}
      </div>
    </nav>
  );
}
