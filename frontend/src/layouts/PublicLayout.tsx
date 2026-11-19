import { useEffect } from 'react';
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom';
import { ArrowUpRight } from 'lucide-react';
import '../styles/marketing.css';

export function PublicLayout() {
  const { pathname, hash } = useLocation();
  useEffect(() => {
    document.title = pathname === '/about' ? 'About MIRAI' : 'MIRAI — Real repositories. Measurable AI.';
    if (hash) document.getElementById(hash.slice(1))?.scrollIntoView();
    else window.scrollTo(0, 0);
  }, [pathname, hash]);
  return <div className="marketing">
    <a className="mirai-skip" href="#main-content">Skip to content</a>
    <header className="mirai-header">
      <Link to="/" aria-label="MIRAI home" className="mirai-brand"><img src="/mirai-logo.jpg" alt="MIRAI" width="78" height="75" /></Link>
      <nav aria-label="Main navigation">
        <Link to="/#features">Features</Link>
        <Link to="/#workflow">How it works</Link>
        <NavLink to="/about">About</NavLink>
      </nav>
      <Link className="mirai-button small" to="/dashboard">Open dashboard <ArrowUpRight size={16} /></Link>
    </header>
    <main id="main-content"><Outlet /></main>
    <footer className="mirai-footer">
      <div><Link to="/" className="mirai-wordmark">MIRAI<span>•</span></Link><p>Real repositories. Measurable AI.</p></div>
      <nav aria-label="Footer navigation"><Link to="/about">About MIRAI</Link><Link to="/#features">Features</Link><Link to="/dashboard">Dashboard</Link></nav>
      <p>Built for the Innodata AI Hackathon 2026 · Track 1</p>
    </footer>
  </div>;
}
