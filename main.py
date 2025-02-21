import socket
from time import sleep
from struct import pack

from packets import TcpUsbHeader

DFU_STATES = {
    0x00: "appIDLE",
    0x01: "appDETACH",
    0x02: "dfuIDLE",
    0x03: "dfuDNLOAD-SYNC",
    0x04: "dfuDNBUSY",
    0x05: "dfuDNLOAD-IDLE",
    0x06: "dfuMANIFEST-SYNC",
    0x07: "dfuMANIFEST",
    0x08: "dfuMANIFEST-WAIT-RESET",
    0x09: "dfuUPLOAD-IDLE",
    0x0A: "dfuERROR"
}

GET_DFU_STATE = pack(
    "<BBHHH",
    0xA1,       # bmRequestType: Device-to-Host, Class, Interface
    0x05,       # bRequest: DFU_GETSTATE
    0x0000,     # wValue: not used in GETSTATE
    0x0000,     # wIndex: Interface 0
    0x0001      # wLength: 1 byte (requested)
)

GET_DFU_STATUS = pack(
    "<BBHHH",
    0xA1,       # bmRequestType: Device-to-Host, Class, Interface
    0x03,       # bRequest: DFU_GETSTATUS
    0x0000,     # wValue: not used in GETSTATUS
    0x0000,     # wIndex: Interface 0
    0x0006      # wLength: 6 bytes (requested)
)

GET_DESCRIPTOR = pack(
    "<BBHHH",
    0x80,       # bmRequestType
    0x06,       # bRequest: Get descriptor
    0x0200,     # wValue: not used in GETSTATUS
    0x0000,     # wIndex: Interface 0
    0x0020      # wLength: 0x20 bytes (requested)
)

USB_DIR_IN = 0x80

# Create a TCP/IP socket
server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

# Bind the socket to the address and port
server_address = ('localhost', 1235)
server_socket.bind(server_address)

# Listen for incoming connections
server_socket.listen(1)

def send_in_token(connection, length):
    header_in = TcpUsbHeader(addr=0x01, ep=(0x00 | USB_DIR_IN), flags=0x00, length=length)
    connection.send(header_in.pack())
    print("IN token sent.")
    sleep(0.01)
    read_header(connection)

def pad_data(data, block_size):
    if len(data) % block_size != 0:
        padding = block_size - (len(data) % block_size)
        data += b'\x00' * padding
    return data

def send_dfu_block(connection, block_num, data):
    UPLOAD = pack(
        "<BBHHH",
        0x21,       # bmRequestType
        0x01,       # bRequest: dlLOAD
        block_num,     # wValue
        0x0000,     # wIndex: Interface 0
        len(data)      # wLength
    )
    header_setup = TcpUsbHeader(addr=0x01, ep=0x00, flags=0x01, length=len(UPLOAD))
    connection.send(header_setup.pack() + UPLOAD)
    sleep(0.01)
    header_data = read_header(connection)
    sleep(0.01)

    packets = len(data) // 0x40
    if len(data) % 0x40 != 0:
        packets += 1

    for i in range(packets):
        print("Sending data packet (%d/%d)" % (i+1, packets))
        data_to_send = data[i*0x40:(i+1)*0x40]
        header_data = TcpUsbHeader(addr=0x01, ep=0x00, flags=0x00, length=len(data_to_send))
        connection.send(header_data.pack() + data_to_send)
        sleep(0.01)
        header_data = read_header(connection)

    # Finish the block transfer
    


def send_file(connection, file_name):
    with open(file_name, "rb") as f:
        file_data = f.read()

    print("File data length: %d" % len(file_data))

    num_blocks = len(file_data) // 0x800 + 1
    for i in range(num_blocks):
        block_data = file_data[i*0x800:(i+1)*0x800]
        print("Sending block %d (%d bytes)" % (i, len(block_data)))
        send_dfu_block(connection, i, block_data)

    print("Sending 0-byte input packet")

    UPLOAD = pack(
        "<BBHHH",
        0x21,       # bmRequestType
        0x01,       # bRequest: dlLOAD
        num_blocks,     # wValue
        0x0000,     # wIndex: Interface 0
        0x0000      # wLength: 0x0 bytes (requested)
    )
    header_setup = TcpUsbHeader(addr=0x01, ep=0x00, flags=0x01, length=len(UPLOAD))
    connection.send(header_setup.pack() + UPLOAD)
    sleep(0.01)
    header_data = read_header(connection)
    sleep(0.01)

    print("Sending DFU status packet (1/2)")
    get_dfu_status(connection)

    print("Sending DFU status packet (2/2)")
    get_dfu_status(connection)

    print("Resetting USB bus")
    
    TCP_USB_RESET = 0x02
    # Construct a header with the reset flag and no additional payload.
    reset_header = TcpUsbHeader(addr=0x01, ep=0x00, flags=TCP_USB_RESET, length=0)
    connection.send(reset_header.pack())
    print("USB reset token sent.")
    sleep(0.01)
    header_data = read_header(connection)
    sleep(0.01)

def read_header(connection) -> TcpUsbHeader:
    header_data = TcpUsbHeader.unpack(connection.recv(5))
    #print(f"Got header: {header_data.addr} {header_data.ep} {header_data.flags} {header_data.length}")
    return header_data

def get_dfu_state(connection):
    header_setup = TcpUsbHeader(addr=0x01, ep=0x00, flags=0x01, length=len(GET_DFU_STATE))
    connection.send(header_setup.pack() + GET_DFU_STATE)
    print("Get DFU state packet sent.")

    sleep(0.01)
    send_in_token(connection, 0x50)
    header_data = read_header(connection)
    data = connection.recv(header_data.length)
    print(f"DFU State: {data[-1]} ({DFU_STATES[data[-1]]})")

def get_dfu_status(connection):
    header_setup = TcpUsbHeader(addr=0x01, ep=0x00, flags=0x01, length=len(GET_DFU_STATUS))
    connection.send(header_setup.pack() + GET_DFU_STATUS)
    print("Get DFU status packet sent.")
    
    sleep(0.01)
    send_in_token(connection, 0x50)
    header_data = read_header(connection)
    data = connection.recv(header_data.length)
    print(f"DFU Status: {data.hex()}")

def get_descriptor(connection):
    header_setup = TcpUsbHeader(addr=0x01, ep=0x00, flags=0x01, length=len(GET_DESCRIPTOR))
    connection.send(header_setup.pack() + GET_DESCRIPTOR)
    print("Get Descriptor packet sent.")

    sleep(0.1)
    send_in_token(connection, 0x20)
    data = connection.recv(32)[5:]

    # Convert from 16-byte unicode to 32-byte hex
    # TODO

    print(f"Descriptor: {data.hex()}")

while True:
    # Wait for a connection
    print('Waiting for a connection...')
    connection, client_address = server_socket.accept()

    try:
        print(f'Connection from {client_address}')

        sleep(1)
        
        send_file(connection, "data/iBSS.n72ap.RELEASE.dfu")
        #get_dfu_status(connection)
        #get_dfu_state(connection)

        while True:
            data = connection.recv(16)

    except Exception as e:
        raise e

    finally:
        # Clean up the connection
        print("Closing connection.")
        connection.close()
        break
