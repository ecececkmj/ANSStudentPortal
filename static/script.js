document.addEventListener("DOMContentLoaded", () => {
    const hamburgerBtn = document.getElementById('hamburgerToggle');
    const navDrawer = document.getElementById('navDrawer');
    const drawerOverlay = document.getElementById('drawerOverlay');
    const viewSections = document.querySelectorAll('.view-section');
    const bodyElement = document.body;

    function toggleMenu() {
        if (navDrawer) navDrawer.classList.toggle('open');
        if (drawerOverlay) {
            drawerOverlay.classList.toggle('active');
            drawerOverlay.classList.toggle('show');
        }
    }

    if (hamburgerBtn) hamburgerBtn.addEventListener('click', toggleMenu);
    if (drawerOverlay) drawerOverlay.addEventListener('click', toggleMenu);

    // Forceful View Switcher
    window.switchView = function(viewName) {
        sessionStorage.setItem('activePortalTab', viewName);
        
        if (viewName !== 'workspace-container') {
            sessionStorage.removeItem('activeClassroom');
        }

        // 1. FORCE HIDE ALL SECTIONS
        viewSections.forEach(section => {
            section.classList.remove('active-view');
            section.style.display = 'none'; 
        });
        
        // 2. FORCE SHOW TARGET SECTION
        const targetSection = document.getElementById('view-' + viewName);
        if (targetSection) {
            targetSection.classList.add('active-view');
            targetSection.style.display = 'block'; // This guarantees it becomes visible!
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }

        if (viewName === 'home') bodyElement.classList.add('home-background');
        else bodyElement.classList.remove('home-background');
    };

    window.openClassroom = function(subjectId) {
        sessionStorage.setItem('activeClassroom', subjectId);
        window.switchView('workspace-container');
        
        document.querySelectorAll('.classroom-workspace').forEach(ws => ws.style.display = 'none');
        const targetWorkspace = document.getElementById('workspace-' + subjectId);
        if (targetWorkspace) targetWorkspace.style.display = 'block';
    };

    // Load Memory
    const savedTab = sessionStorage.getItem('activePortalTab') || 'home';
    window.switchView(savedTab);
    
    const savedClass = sessionStorage.getItem('activeClassroom');
    if (savedTab === 'workspace-container' && savedClass) {
        window.openClassroom(savedClass);
    }

    // Dashboard Cards
    document.querySelectorAll('.portal-card').forEach(card => {
        card.addEventListener('click', () => {
            const targetView = card.getAttribute('data-target-view');
            if (targetView) window.switchView(targetView);
        });
    });

    // Sidebar Navigation
    document.querySelectorAll('.nav-menu-link').forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            window.switchView(link.getAttribute('data-view'));
            if (navDrawer && navDrawer.classList.contains('open')) {
                toggleMenu(); 
            }
        });
    });
});