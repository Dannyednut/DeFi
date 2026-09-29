from execution.protocol_builders import curve_pool_exchange, curve_router_ng, aerodrome_v2, balancer_batch, syncswap

ZERO='0x0000000000000000000000000000000000000000'

def test_curve_direct_builder():
    c=curve_pool_exchange('0x1',0,1,100,90)
    assert c.function == 'exchange'
    assert c.args == (0,1,100,90)

def test_curve_router_ng_pads_route_and_params():
    c=curve_router_ng('0x1',['0xa','0xb'],[[0,1,1,1,2]],100,90)
    assert len(c.args[0]) == 11
    assert len(c.args[1]) == 5
    assert len(c.args[4]) == 5

def test_aerodrome_builder_shape():
    c=aerodrome_v2('0x1',100,90,'0xa','0xb',False,'0xf','0x9',123)
    assert c.function == 'swapExactTokensForTokens'
    assert c.args[0] == 100 and len(c.args[2]) == 1

def test_balancer_builder_shape():
    c=balancer_batch('0x1',b'1'*32,0,1,100,['0xa','0xb'],(ZERO,False,ZERO,False),[100,-90],123)
    assert c.function == 'batchSwap'
    assert c.args[1][0][2] == 1

def test_syncswap_builder_shape():
    c=syncswap('0x1',[('step',)],90,123)
    assert c.function == 'swap'
    assert c.args[1:] == (90,123)
