# An image derived from ledgerhq/speculos but also containing the bitcoin-core binaries

FROM ghcr.io/ledgerhq/speculos:latest

# install git and curl
RUN apt update -y && apt install -y git curl

# install autotools bitcoin-core build dependencies
RUN apt install -y automake autotools-dev bsdmainutils build-essential ccache git libboost-dev libboost-filesystem-dev libboost-system-dev libboost-test-dev libevent-dev libminiupnpc-dev libnatpmp-dev libqt5gui5 libqt5core5a libqt5dbus5 libsqlite3-dev libtool libzmq3-dev pkg-config python3 qttools5-dev qttools5-dev-tools qtwayland5 systemtap-sdt-dev

# clone bitcoin-core from darosior and compile it
RUN cd / && \
    git clone https://github.com/darosior/bitcoin.git && \
    cd bitcoin && \
    git checkout miniscript_wallet_signing && \
    ./contrib/install_db4.sh `pwd` && \
    export BDB_PREFIX='/bitcoin/db4' && \
    ./autogen.sh && \
    ./configure BDB_LIBS="-L${BDB_PREFIX}/lib -ldb_cxx-4.8" BDB_CFLAGS="-I${BDB_PREFIX}/include" --enable-suppress-external-warnings && \
    make -j "$(($(nproc)+1))" && \
    mkdir bin && \
    cp src/bitcoind src/bitcoin-cli src/bitcoin-tx src/bitcoin-util src/bitcoin-wallet ./bin


FROM ghcr.io/ledgerhq/speculos:latest
COPY --from=0 /bitcoin/bin /bitcoin/bin

# install runtime dependencies for bitcoind
RUN apt update -y && apt install -y libminiupnpc-dev libminiupnpc-dev libnatpmp-dev libevent-dev libzmq3-dev

# Add bitcoin binaries to path
ENV PATH=/bitcoin/bin:$PATH