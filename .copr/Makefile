# COPR "make srpm" build method (W2, 2026-11-24). COPR runs: make -f .copr/Makefile srpm outdir=<dir> spec=<spec>
# Point the COPR package at this repository, method "make srpm", spec packaging/scribesense.spec, committish = a tag.
# The signed extension is a release artifact (never in git): it is downloaded from the GitHub release v<version>,
# reading the version only after rpm-build is installed. A failed download fails the build (a release without the
# extension would remove it from users' browsers on upgrade). To build without it on purpose: make ... CRX=0.
CRX ?= 1
RELEASES ?= https://github.com/AlishaNadeem224/ScribeSense/releases/download

srpm:
	dnf -y install git rpm-build python3-rpm-macros pyproject-rpm-macros curl
	git config --global --add safe.directory '*'
	if [ "$(CRX)" = 0 ]; then \
	    SCRIBESENSE_NO_CRX=1 sh packaging/make-srpm.sh "$(outdir)"; \
	else \
	    v=$$(rpmspec -q --srpm --qf '%{version}' packaging/scribesense.spec) && test -n "$$v" && \
	    mkdir -p extension/dist && \
	    curl -fsSL -o extension/dist/scribesense.crx "$(RELEASES)/v$$v/scribesense.crx" && \
	    sh packaging/make-srpm.sh "$(outdir)"; \
	fi
